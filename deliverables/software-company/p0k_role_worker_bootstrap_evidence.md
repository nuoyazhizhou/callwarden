# P0-K Role Worker Task Contract Bootstrap Evidence

**Task**: `T-1787407700109-f5562c60`  
**Purpose**: append-only bootstrap of a Task Contract revision 1 and governance projections for this single P0-K remediation task.  
**No secrets**: no worker credential, lease token, provider account token, or credential hash is included.

## Preconditions

P0-K was atomically created by the current live HTTP authority with a workspace-1 binding, four pending executor steps, and three legacy role contracts. The immediate read-only projection probe returned `next_action.decision=BLOCKED`, with the daemon's explicit condition:

> `Task Contract 缺失、多版本冲突或 revision 链不连续（无法验证 hash）`

This is the same class of projection gap already observed in P0-J-D. The bootstrap is deliberately limited to P0-K, only if all `task_contract_revisions`, role-contract lineage/revision, and step bindings are absent. It does not modify any historical P0-J, P0-J-D, CLI-02, CLI-03, or MCP-001 projection.

## Independent-reviewer blocker that motivates P0-K

The independent Reviewer classified P0-J/P0-J-D as `reviewer_blocked`. Its source and live-authority observations found that Role Worker enrollment/status, CSPRNG/hash-only credential persistence, schema v60 source migration and `role_worker.enroll/revoke/status` routes exist, but stable Role Worker authorization is not wired into `verdict.submit`, `task.apply`, or `task.close`. It also found that the live debug daemon does not match the controlled `runtime/current` artifact.

Current read-only comparison on the Windows authority confirms this drift:

| Item | Current fact |
|---|---|
| HTTP manifest daemon | `C:\git_work\callwarden\rust_ext\target\debug\cw-daemon.exe` |
| Manifest schema version | `58` |
| Current source `RUST_SCHEMA_VERSION` | `60` |
| `runtime/current/cw-daemon.exe` hash | differs from live debug executable hash |
| P0-K next action before bootstrap | blocked because Task Contract is missing |

P0-K must preserve the existing P0-J-D pre-review deployment remediation and must not deploy until an independent reviewer passes this new P0-K scope.

## Bootstrap authorization and scope

The mutation uses the explicit Task Envelope policy `identity_policy=role_worker_v1`. The reviewer lease is task-scoped to P0-K. The caller is the independent adjudicator Role Worker with a distinct stable worker/instance/credential. Both the local credential and reviewer lease token stay only inside ACL-restricted `%USERPROFILE%\.callwarden\role-sessions\...\credentials.bin`; the daemon is the sole authority that validates them.

The evidence and requested mutation are append-only and fail closed. If P0-K is not empty, if its authority binding differs from `ws-1`, if lease fencing is stale, or if the adjudicator worker credential/role does not validate, the daemon must reject without partial projection writes.
