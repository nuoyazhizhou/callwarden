# G0 Artifact Schemas

## Candidate manifest

Each candidate must contain:

```json
{
  "task_id": "...",
  "pair_id": null,
  "pair_slot": null,
  "expected_group": null,
  "strata_key": "profile|risk|diff_size|language|reviewer_model_pair",
  "evidence_class": "source_implementation",
  "evidence_paths": [],
  "required_paths": [],
  "scope_contract": "...",
  "source_commit_hash": "...",
  "diff_sha256": "...",
  "independent_defect_evidence": false,
  "exclusion_reason": null
}
```

Do not include TP, FP, misses, duration, tokens, or verdict in a Creator manifest.

## Evidence manifest

```json
{
  "batch_id": "...",
  "reviewer_home": "absolute path",
  "review_started": false,
  "review_record_count": 0,
  "artifacts": [
    {"path": "absolute path", "sha256": "...", "byte_size": 0, "record_count": 0}
  ]
}
```

## Decision record

Every stage report must contain `decision`, `stage`, `checked_at`, `inputs`, `counts`, `hashes`, `commands`, `exit_codes`, `findings`, and `next_allowed_role`. Allowed decisions are `PASS_TO_CREATOR`, `PASS_TO_REVIEWER`, `PASS_FINAL`, and `FAIL_CLOSED`.
