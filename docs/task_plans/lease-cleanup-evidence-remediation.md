# Lease cleanup evidence remediation

## Correct task-owned evidence capture

- target: docs/evidence/authority-recovery/T-1786983366974-8811ccec-sub-1-remediation.md
- Record the immutable remediation scope, the reviewed implementation path, the focused test command/result, and the exact evidence hash without changing the historical failed/review task records.
- Verify the new evidence path is captured by the daemon task report and that the remediation task reaches review with no shared-worktree broad capture.

Forbidden: direct SQLite edits, history rewrite, changing the historical task target, broad capture, apply/close, or modifying unrelated files.
