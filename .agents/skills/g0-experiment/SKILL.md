---
name: g0-experiment
description: This skill should be used when coordinating, creating, recovering, or independently reviewing Call Warden G0 blind-review experiments; it enforces role separation, provenance, immutable evidence bindings, paired sampling, and fail-closed gates.
---

# G0 Experiment Skill

## Purpose

Apply the repository's G0 protocol consistently across Recovery Agent, Batch Creator, Independent Reviewer, and Coordinator work. Prevent role mixing, stale database use, provenance substitution, reviewer contamination, incomplete handoff bundles, and accidental mutation of historical evidence.

## Use When

- Recovering Git or SQLite evidence for a G0 candidate pool.
- Building or freezing a paired G0 batch.
- Handing a batch to an independent Reviewer.
- Running blind review, reveal, metrics, incident, invalid, or final report workflows.
- Auditing a prior G0 run or deciding whether P1 eligibility is allowed.

## Required Reading

1. Read `docs/design/g0-experiment-protocol-v1.md` before acting.
2. Read `references/role-workflows.md` for the assigned role only.
3. Read `references/evidence-contract.md` before accepting any artifact.
4. Read `references/artifact-schemas.md` and `references/preflight-checklist.md` when validating a handoff.
5. Treat `AGENTS.md`, code, and immutable evidence bindings as higher priority than conversational claims.

## Universal Procedure

1. Identify the role, batch id, authoritative home, database source, and current protocol state.
2. Create or locate the cw task before doing work; keep task steps and target files explicit.
3. Snapshot paths, hashes, sizes, record counts, and session identity before reading mutable evidence.
4. Use a separate home and session for each role. Never use the default experiment home when a batch-specific binding exists.
5. Run the role-specific preflight before writing anything.
6. Stop with `FAIL_CLOSED` on any missing, ambiguous, stale, contaminated, or unbound evidence.
7. Write only artifacts permitted for the current role and stage.
8. Verify hashes, record counts, statuses, and exit codes after writing.
9. Report the exact decision and hand off read-only artifacts to the next role.

## Forbidden Shortcuts

- Do not replace an unresolvable commit with a content-identical commit.
- Do not use estimated duration or token counts.
- Do not manually fill TP/FP/misses, nontrivial, high-risk, or verdict fields.
- Do not repair old JSONL/report/manifest in place.
- Do not treat `evidence_repair` as original implementation evidence.
- Do not continue after a disclosure incident without the protocol's explicit recovery path.
- Do not apply or close tasks from an implementation or Creator session.

## Reference Map

- Recovery: `references/role-workflows.md#recovery-agent`
- Creator: `references/role-workflows.md#batch-creator`
- Reviewer: `references/role-workflows.md#independent-reviewer`
- Evidence types and hash binding: `references/evidence-contract.md`
- Machine-checkable artifacts: `references/artifact-schemas.md`
- Gate checklist: `references/preflight-checklist.md`
