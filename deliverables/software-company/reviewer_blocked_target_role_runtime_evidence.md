# Handoff target-role provenance runtime evidence

- Task: `T-1788018321776-b95d69ec`
- Follow-up source: `T-1788017672961-a901c788`
- Finding reproduced on live daemon event `6036`: `handoff_event_id` and `from_role` projected, but `target_role` was `null` because the persisted envelope contained only `next_role`.
- Fix: persist canonical `target_role` alongside the routing compatibility field `next_role`, using the same validated route.
- Regression: `test_task_level_reviewer_blocked_handoff_creates_fix_defect` passed and asserts both envelope fields.
- Historical task events, verdicts, Reviewer state, and SQLite rows were not manually edited.

Runtime refresh and a new live `task.handoff` / `task.next_action` round-trip will be recorded after the implementation is deployed.
