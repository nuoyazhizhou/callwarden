# T-1787800631870-d7d6ece0 governance protocol skill evidence

Updated `.agents/skills/cw-task-loop/SKILL.md` to:

- require `python cw.py task next-action <task_id> --workspace-instance-id <instance_id> --json`;
- render `lifecycle_status`, `workflow_status`, roles, next action, review summary,
  and blocking reasons;
- distinguish `READY` availability from a completed claim/review/apply/close event;
- map Reviewer PASS to `adjudication_pending` and BLOCKED to
  `remediation_pending`;
- preserve exact task id binding and forbid client-side status guessing.

Static consistency verification passed with role-specific required-field checks;
`git diff --check` passed with only pre-existing CRLF warnings.
