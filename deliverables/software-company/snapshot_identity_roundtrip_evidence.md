# Task-bound snapshot identity round-trip evidence

- Task: `T-1788055266079-7d76f734`
- Commit: `9e68220358da0c3aa1fa68a0447e42d05759c7cc` (`9e68220`)
- Step: `S-1788055266080-7d8310dc`
- Runtime refresh: `20260830-102024-9e68220358da-86ba6aea`
- Runtime daemon PID: `21424`
- Runtime daemon binary: `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe`
- Runtime daemon SHA-256: `61d1093cebed50527c339402fd3973422ee5f4db31a7e2fc191b87556351b445`

## Implementation

The Python daemon clients now pass the current checkout Git remote and HEAD to
`workspace.register`, retain the authority-returned `snapshot_id`, pass it to
`snapshot.publish`, and fail closed if publish returns a different identity.
The Rust `snapshot.publish` handler now inherits the registered identity when
the caller omits it, rejects identity drift, and returns `snapshot_id` in the
publish response. No SQLite history, verdict, or evidence row was edited.

## Focused verification

```text
tokenslim run pytest -q tests/test_snapshot_identity_roundtrip.py tests/test_workspace_snapshot_binding.py
5 passed

tokenslim run cargo test --manifest-path rust_ext/Cargo.toml snapshot_state::tests --lib
51 passed; 0 failed
```

## Live authority round-trip

Using the refreshed daemon and the authority DB
`C:\Users\wanpi\.callwarden\callwarden.db`:

```text
workspace.register
workspace_instance_id = 38c6bf0d73637f85
snapshot_id = 9d3c921779837672
git_head_commit_sha = 9e68220358da0c3aa1fa68a0447e42d05759c7cc

snapshot.publish
workspace_instance_id = 38c6bf0d73637f85
snapshot_id = 9d3c921779837672
symbol_count = 95771
call_count = 125449

HttpDaemonRpcClient._ensure_remote_snapshot
workspace_instance_id = 38c6bf0d73637f85
snapshot_id = 9d3c921779837672
```

`workspace.status` returned the same `workspace_instance_id` and
`snapshot_id`. The negative test verifies that a publish response with a
different snapshot identity raises `E_SNAPSHOT_ID_MISMATCH`.
