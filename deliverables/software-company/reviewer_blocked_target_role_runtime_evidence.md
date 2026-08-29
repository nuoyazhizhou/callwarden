# Handoff target-role provenance runtime evidence

- Task: `T-1788018321776-b95d69ec`
- Follow-up source: `T-1788017672961-a901c788`
- Finding reproduced on live daemon event `6036`: `handoff_event_id` and `from_role` projected, but `target_role` was `null` because the persisted envelope contained only `next_role`.
- Fix: persist canonical `target_role` alongside the routing compatibility field `next_role`, using the same validated route.
- Regression: `test_task_level_reviewer_blocked_handoff_creates_fix_defect` passed and asserts both envelope fields.
- Historical task events, verdicts, Reviewer state, and SQLite rows were not manually edited.

Runtime refresh `20260829-234759-b19d607bc2a8-ad3a6934` passed from commit
`b19d607bc2a81759b03be81121ac7b4a9ffb44b1`; the new daemon PID is `16292` and
the deployed `cw-daemon.exe` SHA-256 is
`7b62bb25e34074d1eb836cd05b34fb6eafce3aa5e84cb12922069d124d77fd94`.
The official refresh smoke/ping passed.

The final live handoff request is
`handoff-target-role-followup-20260829`; its deterministic envelope id is
`he-676a594b54831ef74699604b`. The authoritative projection must expose
`from_role=executor`, `target_role=reviewer`,
`step_id=S-1788018321776-b9668c34`, and
`matches_current_routing=true` after that handoff.
