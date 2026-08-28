# SRV-015 zero-authority evidence

- Task: `T-1787323461541-f7e6ec24`
- Scope: schema migrator authority and thin Python adapter
- Executor: `executor-workbuddy-v1-cur`
- Session: `dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- Workspace: `workspace_id=376`, `workspace_instance_id=4baea3ff12c2ea5c`
- Task contract: `sha256:2f676a27e6a90d267650ec2acf79199a8cd59d8d39c41269d4de16d81df9ca17`
- Role contract: `sha256:42c8dc61c7083b98c5e490520df731893e796a67834cf6c50639511e42c3f8a9`
- Canonicalization: `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## Implementation evidence

Rust `schema_migrator_handlers.rs` owns four RPC methods:

- `mcp.schema_migrator.apply_migrations`
- `mcp.schema_migrator.get_current_version`
- `mcp.schema_migrator.get_migration_history`
- `mcp.schema_migrator.validate_schema`

The handler performs registry/audit migrations, version/history reads, and read-only
table/index validation in Rust. Dispatch and HTTP capability registration are wired
with owner `T-1787323461541-f7e6ec24#SRV-015`.

`server/schema_migrator.py` is now a thin adapter. Its compatibility dataclasses and
startup helpers serialize RPC parameters and normalize results only. The module has
no `sqlite3` import, SQLite connection, `get_db()` call, SQL literal, DDL/DML, or
Python business fallback. The daemon-unavailable path propagates the RPC failure and
does not create a local database.

## Verification

```text
cargo test --manifest-path rust_ext/Cargo.toml schema_migrator_handlers --lib -- --nocapture
3 passed; 0 failed; 1498 filtered out

python -m pytest tests/test_srv_015.py -q
9 passed

python -m py_compile server/schema_migrator.py
AST scan: sqlite_connect_calls=[]; contains_sql_literals=False
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

A real HTTP call to `mcp.schema_migrator.get_current_version` returned
`method_not_found`, proving the running daemon predates this capability. No daemon
replacement, direct SQLite access, or Python fallback was used; deployment remains
pending daemon restart/refresh.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: independently review the SRV-015 whitelist, Rust migration semantics, HTTP registry, Python AST scan, and runtime deployment boundary
  reason: focused Rust/Python tests pass and server/schema_migrator.py has no local DB authority; live daemon is healthy but predates the new methods
  independence_requirement: required
```
