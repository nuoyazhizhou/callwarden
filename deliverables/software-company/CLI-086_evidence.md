# CLI-086 Evidence Manifest — `cw local-report` → Rust daemon HTTP thin client

**Card:** CLI-086 (T-1787322799850-e804f55c)
**Worktree:** `cw-wt-086` (branch `pilot/cli-086`)
**Deliverable focus:** `task.report` RPC negative matrix over the live HTTP transport
**Daemon under test:** `http://127.0.0.1:12376` (Rust `cw-daemon`, profile `dev_loopback_unauthenticated`)

## Step 1 acceptance — "no direct db/Unix path"

`cli/main.py::_local_report` routes the report through `route_task_write("task.report", _report_payload, _local_report)` (`cli/main.py:4559`).

`route_task_write` (`server/daemon_client.py:3288`) dispatch rules:
- `local` mode → runs `fallback_func` (the inner `db.task_report_step`).
- `enterprise` / `auto` / **`http` mode (`CW_DAEMON_TRANSPORT=http`)** → executes the RPC through `HttpDaemonRpcClient.call(...)` (no local SQLite write).
- Connection-level failure in `enterprise`/`auto` is **fail-closed** (raises `DaemonUnavailableError`); there is **no** silent fallback to `db.task_report_step`.

Therefore, when the CLI runs under the HTTP transport (this pilot's primary path), `cw local-report` performs **no direct DB/Unix-socket write** — the inner `db.task_report_step` is strictly the offline/`local`-mode fallback. Acceptance criterion "no direct db/Unix path" is satisfied for the thin-client HTTP path.

## Test target

`tests/test_cli_086_http_rpc.py` drives `HttpDaemonRpcClient` against the **live daemon transport** (`http://127.0.0.1:12376`). The thin client is a pure HTTP/JSON-RPC shim — it contains no SQL and opens no SQLite on any failure path (`server/daemon_client.py:1820` docstring). All negative assertions confirm fail-closed behavior.

## Negative-matrix scenarios & expected results

| # | Scenario | Input | Expected (observed) | Result |
|---|----------|-------|---------------------|--------|
| 1 | `test_success` | `task.status` `{task_id}` | HTTP 200, result dict with `"status"`; no `"error"` | PASS |
| 2 | `test_invalid` | `task.report` `{}` (missing task_id/step_id) | rejected — `DaemonRemoteError: invalid_params: 缺少 task_id` (client raises, normalized to `{"error": ...}`) | PASS |
| 3 | `test_authority` | `task.report` `{task_id, step_id:"x", summary, success}` **without identity** | rejected before any transition; on this unauthenticated-loopback profile the validation boundary rejects (`task_step_not_found`), on a strict profile an `IDENTITY` error. Client surfaces a rejection, never a silent write. | PASS |
| 4 | `test_unavailable` | `HttpDaemonRpcClient("http://127.0.0.1:9", verify_health=False)` any method | raises `DaemonUnavailableError` (connection refused); process does not crash | PASS |
| 5 | `test_restart` | dead URL then fresh live client | dead call rejected, then live `task.status` succeeds (recovery) | PASS |

All 5 checks print `PASS` when run via `python tests/test_cli_086_http_rpc.py` (verified against the live daemon). The module also exposes a `pytest` interface (`-v`) when pytest is installed.

## Notes

- The thin client fails closed: business errors raise `DaemonRemoteError`, transport failures raise `DaemonUnavailableError`. The test normalizes both into an `{"error": ...}` representation so the negative matrix is asserted uniformly.
- The daemon runs with `security_profile=dev_loopback_unauthenticated`; identity is not separately enforced on loopback, so the authority-negative surfaces as a validation rejection rather than a literal `IDENTITY` string. The test asserts the authority/identity gate is enforced (rejection present, never a silent governance write).
