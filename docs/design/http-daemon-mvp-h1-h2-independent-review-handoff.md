# H1/H2 orphaned workspace recovery and Independent Reviewer handoff

## Decision

**REQUEST_CHANGES / NOT REVIEW READY.** The abandoned workspace contains substantial H1 and H2 implementation work and the focused unit suites pass, but neither authoritative task is complete or evidence-bound. Independent Reviewer must not issue PASS from this snapshot.

The next execution role should be a recovery `implementer` for H1/H2. After the findings below are fixed, the original task steps are reported with fresh evidence, and both tasks reach `review`, a distinct `independent_reviewer` may perform the acceptance review.

This document is recovery evidence only. It does not report, apply, close, or reopen H1/H2 and does not attribute the abandoned changes to the recovery coordinator.

## Review snapshot

| Item | Observed value |
|---|---|
| Workspace | `C:\git_work\callwarden` |
| Git HEAD at audit | `90593df31f15341b1897ac6443cdcca86a5ebc99` |
| Python | `C:\Python314\python.exe`, 3.14.3 |
| Daemon | healthy, PID 44824, schema 50 |
| H1 | `T-1786590214634-9e740cdc-sub-2`, `in_progress`, 0/4; step 0 `in_progress`, steps 1-3 `pending` |
| H2 | `T-1786590214634-9e740cdc-sub-3`, `in_progress`, 0/4; step 0 `in_progress`, steps 1-3 `pending` |
| H1/H2 evidence | claim and contract events only; no step reports, result hashes, or transition to `review` |
| Recovery evidence task | `T-1786656000183-82bce750` |

The live daemon binary is not the audited H1 build: `runtime/current/cw-daemon.exe` SHA-256 is `F706872B35A65A4E526B47A0F857A43DE0C024BEB14132A03045E90D2D2D41A8`, while the existing debug build SHA-256 is `CAB552831CECEBFE7FDB049C17CD771EFA1397555A67D4914D1DE951E379955D`. Therefore daemon health is not H1 runtime evidence.

## H1 audit

### Work found

The workspace contains a new Axum/Hyper/Tokio HTTP transport, JSON-RPC endpoints, loopback checks, size/content-type/protocol guards, jobs/cancellation scaffolding, capability data, deduplication scaffolding, manifest publication, and daemon wiring. Focused Rust unit tests pass.

Relevant dirty paths include:

- `rust_ext/Cargo.toml`
- `rust_ext/Cargo.lock` (outside the current H1 allowed paths)
- `rust_ext/src/daemon/http_server.rs`
- `rust_ext/src/daemon/server.rs`
- `rust_ext/src/daemon/mod.rs`
- `rust_ext/src/bin/cw_daemon.rs`
- `rust_ext/_h1_build.bat` (untracked and outside the current H1 allowed paths)

The contract-required `tests/test_http_daemon_transport.py` was not found.

### Blocking findings

1. **P0 — HTTP mutations do not share the legacy daemon serialization point.** The legacy server and HTTP transport construct separate `SerializationPoint` instances. This violates the frozen single-writer/serialization boundary and can permit HTTP and Named Pipe/UDS mutations to execute concurrently.
2. **P0 — request deduplication is not contract-complete.** Dedup state is process-local and is explicitly not persisted across restart. A concurrent duplicate can observe the placeholder result and dispatch again, so the implementation does not prove exactly-once mutation behavior for `(workspace, method, request_id, params_hash)`.
3. **P1 — H1 and H2 disagree on the manifest path.** H1 publishes `http-manifest-v1.json`; H2 discovers `http-daemon.<authority>.manifest.json`. Default discovery therefore cannot bootstrap the H1 server.
4. **P1 — manifest replacement is not atomic on Windows.** The implementation removes the destination before rename, creating a missing-file window. Owner-only Windows ACL behavior required by the frozen contract is also unproven.
5. **P1 — required process-level acceptance evidence is absent.** There is no fresh isolated daemon launch, manifest discovery, real HTTP round trip, stale-manifest test, non-loopback refusal, timeout/cancel/job flow, restart/dedup test, or proof that the audited binary is the live runtime.

### H1 verification run

| Command | Exit | Result |
|---|---:|---|
| `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --lib http_server` | 0 | 10 passed, 0 failed |
| `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --bin cw-daemon` | 0 | check passed; warnings only |
| debug `cw-daemon.exe --help` | 0 | `--http-bind` present |
| `dumpbin /dependents` on debug daemon | 0 | native dependencies shown; no Python DLL dependency |

These results establish source-level compilation and focused unit behavior only. They do not satisfy the H1 process/runtime gate.

## H2 audit

### Work found

The workspace contains HTTP configuration constants, loopback validation, manifest parsing/hash/stale checks, an `HttpDaemonRpcClient`, and two focused test files.

Relevant dirty paths include:

- `config.py`
- `server/daemon_client.py`
- `server/daemon_autostart.py`
- `tests/test_http_daemon_client.py` (untracked)
- `tests/test_http_manifest_discovery.py` (untracked)
- `cli/main.py` (outside the current H2 allowed paths)

The contract-owned `cli/daemon_commands.py` has no corresponding H2 change.

### Blocking findings

1. **P0 — the HTTP client is not selected by the production factory.** With `CW_DAEMON_TRANSPORT=http`, `get_daemon_client()` still returns the legacy `DaemonClient`; `HttpDaemonRpcClient` is referenced by tests but is not wired into production routing. Probe result: `http_enabled=True`, `factory_class=DaemonClient`, `is_http_client=False`.
2. **P0/P1 — out-of-scope direct SQLite fallback was added in `cli/main.py`.** `_local_contract_get()` opens SQLite directly. This path is outside the H2 allowed files and conflicts with the H2 prohibition on direct `CodeGraphDB`/SQLite fallback. It must be removed from this change or handled by a separately authorized task without weakening the HTTP no-fallback rule.
3. **P1 — manifest discovery cannot interoperate with the current H1 publisher.** See the cross-component path mismatch above.
4. **P1 — focused tests use fake HTTP servers only.** They validate parsing/client behavior but do not prove production selection, no-fallback routing, or interoperability with the Rust daemon.

### H2 verification run

| Command | Exit | Result |
|---|---:|---|
| `tokenslim run -- C:\Python314\python.exe -m pytest tests\test_http_daemon_client.py tests\test_http_manifest_discovery.py -q` | 0 | 31 passed |
| `tokenslim run -- C:\Python314\python.exe -m pytest tests\test_windows_daemon_acceptance.py -q` | 0 | 10 passed |
| `C:\Python314\python.exe -m py_compile config.py server\daemon_client.py server\daemon_autostart.py cli\main.py tests\test_http_daemon_client.py tests\test_http_manifest_discovery.py` | 0 | passed |

## Workspace integrity

- `git diff --check` exited 0; only line-ending warnings were emitted for existing documentation changes.
- `git diff --cached --check` exited 0.
- The working tree also contains H0 documentation and unrelated shared changes. A recovery implementer must preserve them and must not reset, overwrite, or claim them.
- No production file was edited during this recovery audit.
- The first Cargo test invocation accidentally forwarded `--nocapture` to Cargo and exited 1 before tests; the corrected command above passed. The invocation error was recorded under the project tool-error policy.

## Required recovery before Independent Review

1. Acquire the correct H1/H2 implementer identity and leases; confirm current role contracts and allowed paths before editing.
2. Resolve out-of-scope files explicitly. Do not silently absorb `Cargo.lock`, `_h1_build.bat`, or `cli/main.py` into H1/H2 ownership.
3. Make HTTP and Named Pipe/UDS share one daemon serialization point.
4. implement persisted and concurrency-safe mutation deduplication, with concurrent duplicate and restart tests.
5. Use one authority-scoped manifest filename/schema in H1 and H2, and implement a genuinely atomic, owner-restricted Windows publication path.
6. Wire `HttpDaemonRpcClient` into the production client factory when HTTP is explicitly selected; prove HTTP mode never falls back to SQLite, Named Pipe, or UDS.
7. Add the contract-owned H1 transport test and production-selection/no-fallback H2 tests.
8. Build and launch a fresh isolated daemon without replacing or restarting the shared runtime; run real-process H1/H2 interoperability and negative acceptance tests with isolated manifest/endpoint state.
9. Record exact HEAD, dirty-path ownership, binary and manifest hashes, commands, exit codes, and runtime evidence in each original H1/H2 step. Only the legitimate implementer may report those tasks to `review`.
10. Hand the resulting reviewed snapshot to a distinct Independent Reviewer. H2I remains the later H1/H2 real-process integration gate and must not be bypassed.

## Independent Reviewer disposition for this snapshot

Expected decision: **REQUEST_CHANGES / BLOCKED**, not PASS. The objective evidence supports “partial implementation with passing focused tests,” not “H1 and H2 completed.”
