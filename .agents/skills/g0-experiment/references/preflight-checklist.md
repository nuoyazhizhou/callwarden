# G0 Preflight Checklist

## Recovery gate

- [ ] Trusted DB path is explicit and copied locally.
- [ ] Source/copy SHA-256, size, schema and integrity check match.
- [ ] Active or contaminated DB is quarantined.
- [ ] Git clone/mirror is full and isolated.
- [ ] Exact source commits resolve, or candidates are excluded.
- [ ] No current worktree, production DB, or historical artifact changed.

## Creator gate

- [ ] At least 48 unique eligible candidates.
- [ ] At least 12 independent defect-evidence candidates.
- [ ] Paired Control/Treatment defect coverage is at least 6/6.
- [ ] 16 unique pair ids and 16/16 groups.
- [ ] Full strata keys match inside every pair.
- [ ] Scope contracts, required paths and non-empty diffs pass.
- [ ] Creator and Reviewer homes are independent real directories.
- [ ] Initial JSONL contains only current pre-verdict blind views.
- [ ] Manifest/evidence hashes, byte sizes and record counts are bound.

## Reviewer gate

- [ ] Reviewer uses a fresh session and batch-specific home.
- [ ] Handoff and blind-package hashes match before first sample.
- [ ] No prior verdict, notes, report or disclosure leaks into Treatment views.
- [ ] Duration is measured, not estimated.
- [ ] Treatment verdict is sealed before reveal.
- [ ] Incidents and invalid samples pause or exclude exactly as protocol requires.
- [ ] Final bundle is separate from immutable handoff artifacts.
- [ ] Report preserves raw denominators and undefined recall.

## Stop conditions

Stop immediately for missing provenance, unresolved hash, scope drift, empty diff, stale/mixed JSONL, path aliasing, contamination, token estimation, manual metric injection, nonzero validation exit, or role boundary violation.
