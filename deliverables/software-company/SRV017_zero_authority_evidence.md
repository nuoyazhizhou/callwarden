# SRV-017 zero-authority evidence

- Task: `T-1787323461683-0059e5a0`
- Scope: Experiment_Batch_Config P0 Stage_Toggle value-preserving migration
- Executor: `executor-workbuddy-v1-cur`
- Session: `dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- Workspace: `workspace_id=376`, `workspace_instance_id=4baea3ff12c2ea5c`
- Task contract: `sha256:a2f98e422244e47de08d389016eeb849548abe63c11c29f0f37b81fa713917c6`
- Role contract: `sha256:e6d5266e348ead662ab5e9110322aadd6edb55fd121c66d30435d0c4a0f5eb23`
- Canonicalization: `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## Implementation evidence

Rust `stage_toggle_migration_handlers.rs` owns
`mcp.stage_toggle_migration.migrate_p0_toggles`. The daemon validates the
payload, creates its Stage_Toggle schema, preserves each P0 `scope_key` and
enabled value, performs an idempotent migration, appends a migration audit
record, and uses daemon time for `changed_at`. Dry-run validation is handled in
Rust without creating the authority database.

Dispatch and HTTP capability registration are wired with owner
`T-1787323461683-0059e5a0#SRV-017`.

`server/stage_toggle_migration.py` now only locates and parses the legacy
`Experiment_Batch_Config` file and serializes the migration request. It has no
`sqlite3` import, database connection, SQL literal, schema creation, or local
business fallback. Daemon failures propagate without creating a local DB.

## Verification

```text
cargo test --manifest-path rust_ext/Cargo.toml stage_toggle_migration_handlers --lib -- --nocapture
3 passed; 0 failed; 1504 filtered out

python -m pytest tests/test_srv_017.py -q
10 passed; 0 failed

python -m py_compile server/stage_toggle_migration.py
passed

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

A real HTTP call to
`mcp.stage_toggle_migration.migrate_p0_toggles` returned `method_not_found`,
showing that the running daemon predates SRV-017. No daemon replacement,
direct SQLite access, or Python fallback was used; deployment remains pending
daemon restart/refresh.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: independently review the SRV-017 whitelist, scope-preserving Rust migration and audit semantics, HTTP registry, Python AST scan, and runtime deployment boundary
  reason: focused Rust/Python tests pass and server/stage_toggle_migration.py has no local database authority; live daemon is healthy but predates the new method
  independence_requirement: required
```
