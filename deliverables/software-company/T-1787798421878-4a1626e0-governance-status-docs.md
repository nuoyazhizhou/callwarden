# T-1787798421878-4a1626e0 documentation evidence

Updated:

- `docs/architecture.md`: defines the two-layer lifecycle/governance model and
  the canonical workflow statuses.
- `docs/cli_reference.md`: documents the `task.next-action` JSON fields and the
  distinction between Reviewer PASS/BLOCKED and `apply`/`close`.
- `docs/mcp_tools.md`: documents `task_governance_projection` and its daemon-only
  authority rule.

The implementation delivered in this task makes the fields authoritative on
`task.next_action`. The existing `task.governance_projection.get` endpoint is
owned by `task_collab.rs`; its field enrichment is tracked separately by
`T-1787799894830-3cd93b18` so the documentation does not hide that boundary.
