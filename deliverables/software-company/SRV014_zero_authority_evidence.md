# SRV-014 zero-authority evidence

- Task: `T-1787323461464-f351e600`
- Scope: replicator rollback probes and refresh path
- Executor: `executor-workbuddy-v1-cur`
- Session: `dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- Workspace: `workspace_id=376`, `workspace_instance_id=4baea3ff12c2ea5c`
- Task contract: `sha256:32222e7ecce78def0a6cc6b8144589ee5d8ced0df34b43c7b9b4b161102fd5c0`
- Role contract: `sha256:727c52a0fd68ab333a12b1e924c7a07ed48637717db1dde9e19b5efd80d8c10a`
- Canonicalization: `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d`

## Implementation evidence

The Python target symbols in `server/replicator.py` now use daemon RPC only:

- `_is_rust_cas_write_rolled_back` → `mcp.replicator.is_rust_cas_write_rolled_back`
- `_is_rust_replicator_query_rolled_back` → `mcp.replicator.is_rust_replicator_query_rolled_back`
- `daemon_handle_refresh` → `mcp.replicator.daemon_handle_refresh`

`daemon_handle_refresh` retains its compatibility signature, serializes `canonical_bytes`
as `canonical_bytes_hex`, and delegates the refresh authority to the Rust daemon's
existing `handle_workspace_file_refresh` path. The Python target functions contain no
SQLite connection, SQL query, `DB_PATH`, or business fallback.

Rust dispatch and HTTP capability registry entries are present for all three methods.
The Rust rollback handlers query `rollback_config` using the original feature names;
the refresh wrapper preserves daemon-owned ACL, session epoch, canonical bytes, CAS,
merge, manifest, and replicate behavior.

## Verification

Commands run from `C:\git_work\callwarden`:

```text
python -m pytest tests/test_srv_014.py -q
14 passed

cargo test --manifest-path rust_ext/Cargo.toml replicator_handlers --lib -- --nocapture
3 passed; 0 failed; 1495 filtered out
```

The focused Python tests cover success, malformed results, exact RPC parameters,
daemon unavailable, restart/cache recheck, refresh byte serialization, dispatch and
capability wiring, Rust handler semantics, and AST-level no-SQLite checks.

## Runtime fingerprint and deployment boundary

The live daemon health probe returned:

```text
endpoint=http://127.0.0.1:10121
pid=5480
git_commit=578da112401d0f02d450363fb6d702dbdfa826f8
schema_version=60
worker_status=healthy
capability_registry_revision=http-mvp-cap-registry-v1
```

A real HTTP call to `mcp.replicator.is_rust_cas_write_rolled_back` returned
`method_not_found` because the running daemon is an older deployment. No runtime
replacement or SQLite fallback was used; the new capability remains pending daemon
restart/deployment. This is recorded as an environment boundary, not a code-test
failure.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: independently review the SRV-014 whitelist, tests, Rust dispatch/capability registration, and runtime deployment boundary
  reason: focused Python and Rust tests pass; target Python functions no longer hold SQLite/business authority; live daemon fingerprint is healthy but predates the new methods
  independence_requirement: required
```
