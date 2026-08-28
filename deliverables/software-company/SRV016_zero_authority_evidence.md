# SRV-016 zero-authority evidence

- Task: `T-1787323461623-fcc66abc`
- Scope: snapshot GC database authority and thin Python adapter
- Executor: `executor-workbuddy-v1-cur`
- Session: `dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- Workspace: `workspace_id=376`, `workspace_instance_id=4baea3ff12c2ea5c`
- Task contract: `sha256:4ef9652a70a185e7e0fa650f347e321965a9bfea5d515521d81937f0bdc861be`
- Role contract: `sha256:a445d451640109b54e056423ffb33a58e2de3fbbb8436ee44ac905d4baef3657`
- Canonicalization: `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## Implementation evidence

Rust `snapshot_gc_handlers.rs` owns the nine database-backed snapshot GC RPC methods:

- `mcp.snapshot_gc.delete_backup_history_record`
- `mcp.snapshot_gc.delete_expired_audit_logs`
- `mcp.snapshot_gc.delete_migration_log_record`
- `mcp.snapshot_gc.get_registered_snapshot_ids`
- `mcp.snapshot_gc.scan_expired_audit_logs`
- `mcp.snapshot_gc.scan_expired_backup_history`
- `mcp.snapshot_gc.scan_expired_migrations_log`
- `mcp.snapshot_gc.scan_orphaned_workspaces`
- `mcp.snapshot_gc.vacuum_databases`

Dispatch and HTTP capability registration are wired with owner
`T-1787323461623-fcc66abc#SRV-016`. The Rust handlers use explicit read-only
connections for scans and Rust-owned read/write connections for deletion and
VACUUM. The filesystem-only orphaned snapshot scan remains local because it
does not read or write database state.

`server/snapshot_gc.py` is now a thin RPC adapter for all database-backed
operations. It has no `sqlite3` import, connection, SQL literal, `get_db()`
call, or local business fallback. Daemon failures propagate and do not create
a local registry database.

## Verification

```text
cargo test --manifest-path rust_ext/Cargo.toml snapshot_gc_handlers --lib -- --nocapture
3 passed; 0 failed; 1501 filtered out

python -m pytest tests/test_srv_016.py -q
9 passed; 0 failed

python -m py_compile server/snapshot_gc.py
passed

AST/rg scan: sqlite_connect_calls=[]; contains_sql_literals=False
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
`mcp.snapshot_gc.get_registered_snapshot_ids` returned `method_not_found`,
showing that the running daemon predates SRV-016. No daemon replacement,
direct SQLite access, or Python fallback was used; deployment remains pending
daemon restart/refresh.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: independently review the SRV-016 whitelist, Rust snapshot GC semantics, HTTP registry, Python AST scan, and runtime deployment boundary
  reason: focused Rust/Python tests pass and server/snapshot_gc.py has no local database authority; live daemon is healthy but predates the new methods
  independence_requirement: required
```
