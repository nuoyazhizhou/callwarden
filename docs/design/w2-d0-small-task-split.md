# W2/D0 machine task split

## A1 W2.3 P1-B contract review
Read-only production contract review for the five query RPCs. Do not change code or historical evidence.
- inspect @ rust_ext/src/daemon/dispatch.rs
- inspect @ rust_ext/src/daemon/snapshot_state.rs
- test @ tests/test_w2_3_query_uds_e2e.py
- evidence @ g0-reviewer-scratch/w2-d0/a1-contract

## A2 W2.3 Linux UDS evidence
Run a fresh Linux daemon and capture raw process-level UDS evidence. Old evidence is immutable.
- build @ rust_ext/target-review
- test @ tests/test_w2_3_query_uds_e2e.py
- record @ g0-reviewer-scratch/w2-d0/a2-uds
- hash @ g0-reviewer-scratch/w2-d0/a2-uds

## A3 W2.3 attribution audit
Repair only current W2.3 P1-B task attribution and prove file hashes and event ownership.
- query @ cw task evidence
- compare @ g0-reviewer-scratch/w2-d0/a3-attribution
- verify @ change_audit
- report @ g0-reviewer-scratch/w2-d0/a3-attribution

## A4 W2.4 Linux dual UID evidence
Run real Linux root/setuid process tests for isolation, clean/dirty state, and restart recovery.
- build @ rust_ext/target-review
- test @ tests/test_process_level_e2e_recovery.py
- record @ g0-reviewer-scratch/w2-d0/a4-dual-uid
- hash @ g0-reviewer-scratch/w2-d0/a4-dual-uid

## A5 W2.4 attribution evidence
Bind A4 evidence to W2.4. If there is no code change, record evidence-only without inventing a diff.
- query @ cw task evidence
- compare @ g0-reviewer-scratch/w2-d0/a5-attribution
- verify @ task_events
- report @ g0-reviewer-scratch/w2-d0/a5-attribution

## A6 D0 Python 3.14 focused change
Commit only the already identified three D0 files and keep all G0 files outside the commit.
- edit @ cli/main.py
- edit @ docs/design/daemon-deploy-runbook.md
- edit @ docs/design/phase4-4-systemd-dual-uid-container-e2e-contract.md
- test @ g0-reviewer-scratch/w2-d0/a6-d0

## A7 D0 status reconciliation
Read-only status review of the D0 child and parent task tree. Do not batch-update statuses.
- query @ cw task status-tree
- inspect @ g0-reviewer-scratch/w2-d0/a7-status
- verify @ task_steps
- report @ g0-reviewer-scratch/w2-d0/a7-status

## A8 GD gate decision matrix
Review six GD children and state the platform-specific snapshot.publish decision without hiding Windows gaps.
- inspect @ docs/design
- compare @ rust_ext/src/daemon
- verify @ g0-reviewer-scratch/w2-d0/a8-gd
- report @ g0-reviewer-scratch/w2-d0/a8-gd

## A9 independent final review
Review A1 through A8 using source, raw logs, hashes, task ownership, and task states. Do not edit or close.
- review @ g0-reviewer-scratch/w2-d0
- verify @ cw task status-tree
- audit @ change_audit
- report @ g0-reviewer-scratch/w2-d0/a9-review

