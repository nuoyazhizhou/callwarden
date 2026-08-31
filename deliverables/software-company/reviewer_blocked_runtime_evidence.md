# Reviewer provenance runtime follow-up evidence

- Task: `T-1788017672961-a901c788`
- Source implementation task: `T-1788016089952-1647e128`
- Scope: deploy the committed provenance envelope/projection fix and verify the live daemon round-trip. No historical task event, verdict, Reviewer state, or SQLite row was edited.
- Runtime refresh: `20260829-233515-915c5283be5c-d452c5e5`
- Source HEAD at refresh: `915c5283be5c16d7eda66dcdc3355870b2febfa8`
- Endpoint: `\\.\pipe\callwarden-S-1-5-21-1583625257-826939952-3615027596-1001`
- Daemon PID after refresh: `50264`
- Daemon binary SHA-256: `93f3b46733c42e8b4e055995f6e3840a4582c17c1de4390226242e8fb77ad28b`
- Refresh result: official `refresh_shared_runtime.ps1 -Configuration release -RunSmokeTests` passed; daemon ping passed.

For the final live round-trip, the formal handoff request is
`handoff-runtime-followup-20260829` and its deterministic envelope id is
`he-` plus the first 24 hexadecimal characters of
`sha256(T-1788017672961-a901c788:handoff-runtime-followup-20260829)`.
The expected authoritative projection is the same event id, `from_role=executor`,
`target_role=reviewer`, `step_id=S-1788017672962-a90a086c`, and
`matches_current_routing=true`.
