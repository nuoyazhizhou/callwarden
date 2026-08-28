# T-1787800631870-d7d6ece0 governance protocol docs evidence

Updated governance sources:

- `AGENTS.md`
- `Callwarden 无人值守循环启动模板：Executor _ Planner v3.md`
- `Callwarden 无人值守循环启动模板：Reviewer v3.md`
- `Callwarden 无人值守循环启动模板：Adjudicator v3.md`

The documents now define one two-layer model:

- `lifecycle_status` remains the raw daemon state-machine gate.
- `workflow_status` is the daemon-derived human-facing progress stage.

The role procedures explicitly distinguish an available `READY` action from a
completed event, preserve the exact task id through every handoff, and describe
Reviewer PASS/BLOCKED, Adjudicator apply, and close as separate observable
transitions.
