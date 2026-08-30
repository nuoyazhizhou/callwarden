# Workspace authority / snapshot binding post-refresh evidence

Task: `T-1788050221973-114dab10`

This is supplemental evidence for the immutable task evidence reported before
the shared runtime refresh. The original evidence remains unchanged.

## Runtime refresh

```text
pwsh -NoProfile -File scripts/refresh_shared_runtime.ps1 -TaskId T-1788050221973-114dab10 -RunSmokeTests
Runtime refresh completed: 20260830-085058-984917639653-f0e09e0b
```

## Fresh daemon probe and binding round-trip

```json
{
  "ping": {
    "status": "ok",
    "pid": 48028,
    "transport": "http",
    "authority_id": "LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/1e3954ffcf2c773ce3afca4dd4194a30a4f435acb0d0a2eaa89c56cba61a6838",
    "task_db_fingerprint": "1e3954ffcf2c773ce3afca4dd4194a30a4f435acb0d0a2eaa89c56cba61a6838"
  },
  "workspace.status": {
    "workspace_id": 894,
    "workspace_instance_id": "4baea3ff12c2ea5c",
    "client_view_root": "C:\\git_work\\callwarden",
    "status": "active"
  },
  "snapshot.list_workspaces": {
    "workspace_instance_id": "4baea3ff12c2ea5c",
    "generation": 1,
    "history_len": 0,
    "symbol_count": 95771,
    "call_count": 125449,
    "file_count": 1623
  }
}
```

The fresh runtime proves the same authority binding after deployment. The
registry `snapshot_id` remains null because this workspace registration has no
Git remote/head metadata; the daemon snapshot cache is populated and keyed by
the authoritative workspace instance plus generation.
