# Runtime bootstrap evidence — T-1787064771931-96bd983c

## Scope

One-time CallWarden self-bootstrap deployment of the current daemon and PyO3
runtime containing the provenance-aware `task.remediation.create` RPC. This run
did not modify any historical task, verdict, evidence, or handoff record.

## Authoritative deployment

- Script: `scripts/refresh_shared_runtime.ps1`
- Configuration: `release`
- Result: `passed`; `rollback=false`; `error=null`
- Git HEAD recorded by deployment: `6bf7353bef984ecd7065b6820948421b00c0cd4e`
- Runtime version: `20260818-225328-6bf7353bef98-4c6535bc`
- Runtime evidence:
  `C:\Users\wanpi\.callwarden\runtime\evidence\20260818-225328-6bf7353bef98-4c6535bc.json`
- Runtime evidence SHA-256:
  `75748ce04e6b7725fc6c486d84c1b864fddfbdcabc3211684c291e053dcc4083`

## Installed authority

- Running daemon PID: `11500`
- Running path:
  `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe`
- Running/build/runtime SHA-256:
  `0fd2a958ee38e8bc7516acde5a6f5327b2c52f14f61448e7edc398bfd8b22654`
- Daemon dependency mode: `python_free`
- Ping: exit `0`, authority transport `http`, protocol version `1`
- Authority/task DB fingerprint:
  `c2441ea2cd05c2e160794ca8162a8a03f3423ae55501c11b82d8be6548e5e47d`
- Installed `cw.exe`:
  `C:\Users\wanpi\AppData\Roaming\Python\Python314\Scripts\cw.exe`
- Installed `cw.exe` SHA-256:
  `8b0acc0bad0a6903da0b9075d6dae9b12679d044e1445921b1a866f2d5073b37`
- Repository and Python 3.14 site-package PyO3 SHA-256:
  `2038b67d697328a02ca21c6644ed342959a8b5c617193975339ccc4b2730be28`
- Both PyO3 targets depend on `python314.dll`.
- Installed authority CLI verified the active implementer lease
  `L-213797b9a1ec4c83`, fencing counter `1`.

## Real RPC fail-closed round-trip

The running daemon was called through the production client and Named Pipe
authority after deployment. All probes used task
`T-1787064771931-96bd983c`; task events were counted before and after.

1. Unknown `source_outcome` returned
   `E_REMEDIATION_SOURCE_OUTCOME_INVALID`.
2. A canonical `reviewer_blocked` request without lease credentials returned
   `E_LEASE_REQUIRED`.
3. A `failed_step` request with the real active implementer lease and fencing,
   but an in-progress source step, reached domain validation and returned
   `E_FAILED_STEP_NOT_UNRESOLVED`.
4. Event count remained `2 → 2`, the current step projection was unchanged,
   and task status remained `in_progress` after all rejected requests.

These probes prove that the deployed runtime exposes the new source-outcome
parser, validates the protected mutation lease, reaches the new domain branch,
and performs zero task-domain writes on deterministic rejection. A positive
`reviewer_blocked` append is intentionally deferred until a real independent
Reviewer submits the required Verdict Ledger entry and structured handoff; this
Executor did not forge Reviewer or Adjudicator authority.
