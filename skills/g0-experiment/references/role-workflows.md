# G0 Role Workflows

## Recovery Agent

1. Use a clean local clone or isolated mirror; never write to the product worktree `.git`.
2. Use the trusted `callwarden.db.rebuilt` copy in a local home; quarantine the active database if it contains synthetic or unverified records.
3. Verify `integrity_check`, schema, row counts, source hash, and copy hash.
4. Resolve the exact full source commit with `cat-file`; reject alternate content matches.
5. Rebuild read-only before/after provenance under the recovery directory.
6. Produce `PASS_TO_EVIDENCE_REBUILD` only when exact provenance is proven; otherwise produce `FAIL_CLOSED`.
7. Do not call `capture-diff`, write `change_audit`, create a batch, or write Reviewer records.

## Batch Creator

1. Consume only a Recovery `PASS_TO_EVIDENCE_REBUILD` or a trusted current task-attributed evidence set.
2. Preflight at least 48 unique candidates, 12 independent defect-evidence candidates, and 6+6 paired coverage.
3. Reject historical G0 samples, prior Reviewer conclusions, scratch/junction paths, empty diffs, missing scope paths, and unresolved source commits.
4. Create a fresh batch id, seed, protocol fingerprint, Creator home, and independent Reviewer home.
5. Pair by full strata key; enforce unique pair ids and opposite group assignment.
6. Admit with `--scope-contract`; keep Control notes factual and keep Treatment pre-verdict views free of notes/verdict leakage.
7. Write only current-batch blind views before Reviewer start. Use `real` or `unavailable` token status, never estimated.
8. Bind every artifact with absolute path, size, record count, and SHA-256.
9. Deliver a read-only handoff and explicitly state that no Reviewer records or task apply/close were performed.

## Independent Reviewer

1. Use the batch-specific Reviewer home and validate the evidence manifest before opening a sample.
2. Confirm the initial JSONL contains only current-batch pre-verdict blind views and that blind-package hashes match.
3. Review each sample independently. Do not open notes, reveal data, prior reports, or implementation conclusions before the protocol permits it.
4. Record real duration. Let the system calculate nontrivial status.
5. For Treatment, record and seal the pre-reveal verdict before reveal; record changes only after reveal.
6. On contamination, disclosure, malformed view, or missing evidence, record the permitted incident/invalid event and pause as required.
7. Generate final evidence in the permitted final bundle without changing the immutable handoff bundle.
8. Report raw denominators and eligibility. Never turn undefined recall into zero.
9. Do not modify source code, thresholds, protocol, historical batches, or task closure state.

## Coordinator

1. Read the current task state and evidence state independently; do not trust an Agent's completion prose.
2. Route Recovery failures back to evidence recovery, Creator failures back to candidate sourcing, and Reviewer failures to the defined incident path.
3. Start the next role only after the previous role's immutable handoff and gate decision exist.
4. Keep G0 non-product unless the protocol explicitly records a valid eligibility decision.
