# Snapshot publish workspace binding evidence

- Remediation task: `T-1788077285594-4eceeaac`
- Source blocked task: `T-1788067569565-1e5b45ac`
- Scope: `cli/main.py` and focused CLI regression tests only.

## Implementation

`cw collab publish` now performs a daemon-native `workspace.register` for the
requested root, uses the returned authoritative `workspace_instance_id` and
`snapshot_id`, and supplies the authority database path to
`snapshot.publish`. It fails closed when registration does not return an
authoritative workspace identity; it never substitutes a locally derived hash.

## Verification

Focused command:

```text
python -m pytest -q tests/test_cli_collab_snapshot_publish.py tests/test_task_verdict_cli.py
7 passed
```

The live daemon round-trip was executed through the CLI (no direct SQLite,
generic RPC bypass, or verdict submission):

```text
python cw.py collab publish --workspace="C:\\git_work\\callwarden" --json
```

Daemon response:

```json
{
  "ok": true,
  "method": "snapshot.publish",
  "result": {
    "generation": 1,
    "symbol_count": 95771,
    "call_count": 125449,
    "workspace_instance_id": "2d16bcc4c6931e95",
    "snapshot_id": "aebf898bc6614594"
  }
}
```

At capture time, `cw daemon ping` reported PID `11688` and authority/task DB
fingerprint `e606aad47c7139a910c83a09f5e3d96ed6eff3727f0a202cf65b14a918878e27`.
The live runtime binary SHA-256 was
`605583ae2bcb9cea8766f373c385919f7cbd93f159617a9d4fcebba1ee97795a`.

No Reviewer verdict, apply, or close was attempted.
