# Workspace authority / snapshot binding remediation evidence

Task: `T-1788050221973-114dab10`

## Scope

This remediation fixes one binding defect only: HTTP MCP/CLI calls must use the
Call Warden project root rather than the host process working directory, and the
`workspace.status` authority projection must ensure a real daemon snapshot is
published before querying it. No historical task, verdict, evidence, or SQLite
row was edited directly.

## Implementation

- `server/mcp_server.py`: configure the HTTP thin-client singleton with
  `config.PROJECT_ROOT` during MCP server creation.
- `server/daemon_client.py`: use `PROJECT_ROOT` when no explicit workspace is
  configured; `route_rpc("workspace.status", ...)` obtains the authoritative
  daemon DB path and calls `_ensure_remote_snapshot` before injecting the
  daemon-returned `workspace_instance_id`.
- The local `derive_workspace_instance_id` value is not used for the HTTP
  authority binding.

## Tests

```text
tokenslim run pytest -q tests/test_workspace_snapshot_binding.py
2 passed

tokenslim run python.exe -m py_compile server/mcp_server.py server/daemon_client.py
passed

git diff --check -- server/mcp_server.py server/daemon_client.py tests/test_workspace_snapshot_binding.py
passed
```

The focused regression file SHA-256 is:

`sha256:C43D23AC250F12096F0AA9DD37D80B0AE099A1EA64F18D86B3D6A1769D3C0F84`

## Live daemon round-trip

Daemon probe:

```json
{
  "status": "ok",
  "pid": 18900,
  "transport": "http",
  "authority_id": "LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/77ac8d265921eae6ea84ff70b5123dfc1c27f815443eda8ed97950c7f3fd891c",
  "task_db_fingerprint": "77ac8d265921eae6ea84ff70b5123dfc1c27f815443eda8ed97950c7f3fd891c"
}
```

The production route was invoked from the repository while the process cwd was
not used as the binding source. The observed authority projection and snapshot
cache agreed on the same daemon workspace instance:

```text
workspace.status:
  workspace_instance_id = 4baea3ff12c2ea5c
  client_view_root      = C:\git_work\callwarden
  status                = active

snapshot.list_workspaces:
  workspace_instance_id = 4baea3ff12c2ea5c
  generation            = 2
  history_len           = 1
  symbol_count          = 95771
  call_count            = 125449
  file_count            = 1623
```

The registry `snapshot_id` is `null` because this workspace registration has no
Git remote/head metadata; the published snapshot is nevertheless present in
the daemon cache and is identified by the same authoritative workspace instance
and generation. No snapshot or verdict was fabricated.
