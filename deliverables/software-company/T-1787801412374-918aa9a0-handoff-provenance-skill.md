# T-1787801412374-918aa9a0 Handoff provenance skill evidence

Updated `.agents/skills/cw-task-loop/SKILL.md` with a hard output contract:

- `task_id` must be the first Handoff field and must come from daemon `next-action`;
- `request_id`, `step_id`, `report_request_id`, `evidence_path`, and
  `evidence_hash` are mandatory (or explicitly unavailable when applicable);
- identity must include `agent_id`, `agent_instance_id`, `session_id`,
  `model_id`, and `role`;
- missing provenance fails closed instead of emitting ready/pass/accepted;
- a current Handoff cannot schedule claiming a later task.
