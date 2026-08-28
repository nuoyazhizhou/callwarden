# SRV-018 zero-authority evidence

- Task: `T-1787323461742-03e6a000`
- Scope: StagingLog rollback authority and thin Python adapter
- Executor: `executor-workbuddy-v1-cur`
- Session: `dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- Workspace: `workspace_id=376`, `workspace_instance_id=4baea3ff12c2ea5c`
- Task contract: `sha256:5a5ab74b3f1c43b1e99fe9ca6186eb061224801f72383cdbd534841a69e847fe`
- Role contract: `sha256:dd1b0902e490974050d9fd70614d2322de30ddc7fc2c25b16132ce4d45913daa`
- Canonicalization: `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## Implementation evidence

Rust `staging_log_handlers.rs` owns
`mcp.staging_log.is_rust_staging_log_rolled_back`. It reads the latest
`rust_staging_log` rollback flag from the daemon authority database and
returns stable fail-soft `rolled_back=false` results for missing tables,
unavailable databases, or query errors.

Dispatch and HTTP capability registration are wired with owner
`T-1787323461742-03e6a000#SRV-018`.

`server/staging_log.py` now keeps only the rollback cache and calls the daemon
HTTP RPC. The target function has no local SQLite connection, rollback SQL, or
database fallback; daemon failure remains fail-soft and does not create a
local authority database. Other StagingLog file/PyO3 compatibility operations
are outside this task's exact target symbol.

## Verification

```text
cargo test --manifest-path rust_ext/Cargo.toml staging_log_handlers --lib -- --nocapture
4 passed; 0 failed; 1507 filtered out

python -m pytest tests/test_srv_018.py -q
9 passed; 0 failed

python -m py_compile server/staging_log.py
passed

AST scan: target sqlite_connect_calls=[]; target contains rollback SQL=False
```

## Runtime fingerprint and deployment boundary

Live daemon health returned:

```text
endpoint=http://127.0.0.1:10121
pid=5480
git_commit=578da112401d0f02d450363fb6d702dbdfa826f8
schema_version=60
worker_status=healthy
capability_registry_revision=http-mvp-cap-registry-v1
```

A real HTTP call to
`mcp.staging_log.is_rust_staging_log_rolled_back` returned
`method_not_found`, showing that the running daemon predates SRV-018. No
daemon replacement, direct SQLite access, or Python fallback was used;
deployment remains pending daemon restart/refresh.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: independently review the SRV-018 whitelist, Rust rollback flag semantics, HTTP registry, target-function AST scan, and runtime deployment boundary
  reason: focused Rust/Python tests pass and _is_rust_staging_log_rolled_back has no local database authority; live daemon is healthy but predates the new method
  independence_requirement: required
```
