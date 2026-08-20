# CallWarden Codex Agent Orchestrator loop

## 1. Protocol and ownership design

- target: `docs/design/cw-agent-orchestrator.md`
- freeze the external orchestrator boundary, role routing, handoff envelope, lease/workspace authority, retry/idempotency, and stop conditions.
- forbidden: production daemon changes, direct SQLite writes, task status changes outside daemon, apply/close.

## 2. Coordination loop

- target: `orchestrator/coordination.py`
- implement polling, eligible-task selection, bounded concurrency, dispatch deduplication, handoff reconciliation, and retry/backoff.
- depends on task 1.

## 3. Codex agent runner

- target: `orchestrator/codex_runner.py`
- implement Codex App Server session launch, workspace cwd validation, streaming events, timeout/cancellation, and worker termination.
- depends on task 1.

## 4. CallWarden adapter

- target: `orchestrator/cw_adapter.py`
- implement daemon-authoritative task/workspace/lease/next-action/report calls with request-id and fencing propagation; no direct SQLite fallback.
- depends on task 1.

## 5. End-to-end and recovery tests

- target: `tests/test_agent_orchestrator_loop.py`
- verify executor-reviewer-adjudicator routing, blocked remediation, concurrent tasks, crash/lease recovery, duplicate dispatch suppression, and authority fail-closed behavior.
- depends on tasks 2-4.
