# SRV-019 zero-authority gate evidence (blocked)

- Task: `T-1787323461802-077bee78`
- Current step: `S-1787323461804-07978688` (`retire_python_authority`)
- Executor: `executor-workbuddy-v1-cur`
- Session: `dcf88a76-0895-4f09-9245-1cc8cbaedb82`
- Workspace: `workspace_id=376`, `workspace_instance_id=4baea3ff12c2ea5c`
- Task contract: `sha256:9b24e362d18b96033eeab3f3e94326575f34c92aed81d1a915acd7ebaad48bce`
- Role contract: `sha256:0054f9876d8f4bcbcb7d7352027242f31e149b216dd3858ce45cca7426682c79`

## Implemented gate boundary

`rust_ext/src/daemon/dispatch.rs` now routes
`mcp.final_zero_python_authority_audit`. The daemon requires a
`repository-wide` source and non-negative scan counters, returns a structured
`authority_residue` error for any non-zero finding, and returns `passed` only
when both finding counters are zero. The HTTP capability registry advertises
the method as `rust_native`, `read_only`, `/v1/rpc`, owned by this task.

The Python audit utility now performs a read-only AST scan of all `server/*.py`
files, including sqlite imports/connections, known DB helpers, and connection
SQL execution. It writes an auditable JSON snapshot and exits non-zero on any
finding; it has no database access or fallback.

## Verification

```text
tokenslim workspace --format llm
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --lib dispatch
105 passed; 0 failed

python -m py_compile deliverables/software-company/audit_server_authority_residue.py
passed

python deliverables/software-company/audit_server_authority_residue.py
scanned_files=52; finding_count=108; files_with_findings=14; passed=false; exit=1
```

The non-zero result is an intentional fail-closed result, not a test failure.
Representative remaining executable authority includes `server/compat_worker.py`,
`server/daemon_server.py`, `server/health_check.py`,
`server/durable_staging.py`, `server/job_executor.py`, and several
`server/tools/` handlers. Those files are outside this task's allowed edit
scope (`other server modules` and `server/tools/` are forbidden), so this task
cannot truthfully claim final zero residue.

The pre-deployment live daemon was healthy but predates this route:

```text
endpoint=http://127.0.0.1:10121
pid=5480
git_commit=578da112401d0f02d450363fb6d702dbdfa826f8
schema_version=60
worker_status=healthy
capability_registry_revision=http-mvp-cap-registry-v1
mcp.final_zero_python_authority_audit -> method_not_found
```

Before review, the release runtime was deployed with:

```text
scripts/refresh_shared_runtime.ps1 -TaskId T-1787203926824-9f873bfc -Configuration release
runtime evidence: C:\Users\wanpi\.callwarden\runtime\evidence\20260827-021423-c9f8b592d55a-c260d63f.json
endpoint=http://127.0.0.1:12933
pid=34660
git_commit=c9f8b592d55a80a20cd6b598f2f677b40c180e9a
worker_status=healthy
zero-count RPC -> status=passed, authority=rust-daemon
non-zero RPC -> authority_residue (108 findings in 14 files)
```

No direct SQLite fallback was used. The deployment proves the new daemon route,
but does not remove the remaining Python authority findings.

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_blocked_to_user
  next_role: user
  next_action: expand the approved SRV-019 scope or complete the remaining server/tool migrations, then rerun the final zero-authority gate
  reason: the repository-wide AST gate found 108 executable authority residues in 14 files, while the task contract forbids modifying the responsible other server modules and server/tools handlers
  independence_requirement: not_applicable
```
