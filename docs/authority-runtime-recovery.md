# Python core runtime deployment and authority verification

## Why a daemon refresh was insufficient

`scripts/refresh_shared_runtime.ps1` originally built and deployed only the
Call Warden executable binaries under `%USERPROFILE%\.callwarden\runtime\current`.
That is sufficient for `cw-daemon.exe`, but not for the Python extension that
the installed `cw.exe` imports.  In a Python 3.14 editable installation there
can be two live extension targets:

- `callwarden_core.pyd` at the repository root, used by source-path `cw.py`;
- `callwarden_core.callwarden_core.cp314-win_amd64.pyd` in the Python 3.14
  user site-packages directory, used by the installed `cw.exe`.

If only the repository copy is replaced, `python cw.py ...` can succeed while
the authoritative `cw.exe` still loads an old extension and rejects task
mutations with `MIGRATION_FAILED: schema checksum mismatch`.

## Required recovery command

Run from the repository root with a real task ID:

```powershell
pwsh -File .\scripts\refresh_shared_runtime.ps1 `
  -TaskId T-<real-task-id> `
  -RestartMcp `
  -RunSmokeTests
```

The script uses only `C:\Python314\python.exe`.  It builds the Rust library
as well as daemon/CLI binaries, discovers the extension imported by that
interpreter's installed `cw.exe`, and deploys every discovered target with an
atomic replacement.  It saves pre-deployment extension copies under
`%USERPROFILE%\.callwarden\runtime\versions`.

## Acceptance gate

The refresh fails and rolls back the extension targets if any of these checks
does not pass:

1. Built and deployed extension SHA-256 values differ.
2. A deployed extension links a Python DLL other than `python314.dll`.
3. The installed Python 3.14 `cw.exe` cannot run
   `lease status <TaskId> --role implementer` without a migration checksum
   mismatch.
4. The daemon executable hash/path/runtime checks fail.

Do not treat `python cw.py ...` alone as authority recovery.  Record the
runtime evidence JSON and verify the installed `cw.exe` command before
acquiring a lease, claiming work, reporting a step, applying, or closing a
task.

## Remediation attribution

Task `T-1786966646248-6f1c0890-sub-1` owns only the runtime deployment fix and
its acceptance record.  The frozen baseline is Git `6bf7353bef984ecd7065b6820948421b00c0cd4e`;
the owned script changed from blob `c8a4b3ce8748d4f307f0b4cc629cf31c7c1535d0`
to `6e9cdd7283897308ef8f4c30d5ec6e52cc06910b`.  The refresh evidence is
`20260817-212221-6bf7353bef98-53298375.json` (SHA-256
`0b978156ee2ec6a30c7f52e51243fdefa007aa36fadd69630b7dfd2709b9c059`).

The existing `AGENTS.md` dirty diff and all frozen/excluded paths are not
claimed by this remediation.  No source/schema/migration, SQLite, WAL/SHM,
H4BC, design-document, or G0-scratch change is part of this attribution.
