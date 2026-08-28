# T-1787801412374-918aa9a0 Handoff provenance docs evidence

Updated `AGENTS.md` and the three v3 role templates.

The sources now require every user-facing or downstream Handoff to begin with
the exact daemon-selected `task_id` and include `request_id`, `step_id`,
`report_request_id`, `evidence_path`, `evidence_hash`, and the complete
five-field identity. They also prohibit putting “claim the next task” inside
the current task's `next_action`; the next task is discovered only after the
current task has been handed off.

The previous incomplete Handoffs remain historical and are not rewritten.
