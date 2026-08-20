# G0 Evidence Contract

## Required provenance fields

Every candidate implementation evidence record must identify:

```text
task_id
step_id
file_path
source_commit_hash or working_tree_generation
hash_before
hash_after
non_empty_diff
scope_contract
agent_id
session_id
recorded_at
```

`source_commit_hash` must be a full, resolvable commit. A content-identical alternate commit is not an acceptable replacement.

## Evidence classes

`source_implementation` proves a task-attributed current change. `historical_provenance` proves an old source and must retain the original hash. `evidence_repair` proves only a later attribution repair. `reviewer_observation` is produced during blind review and cannot be used for Creator prefill.

## Artifact binding

Bind each authoritative artifact using:

```text
absolute_path
sha256
byte_size
record_count
batch_id
created_at
review_started
```

Reject a binding if the path is missing, points through a junction/symlink, points to a different home, changes after handoff, or contains records from another batch.

## JSONL phases

Before review: current batch `pre_verdict` `blind_view` only.

During review: add only protocol-permitted metrics, verdict, reveal, invalid, and incident records.

After review: produce final report/evidence in a final bundle without rewriting the immutable handoff bundle.

## Database rules

Use a trusted snapshot copied to a local writable home. Preserve source DB, WAL, and SHM. Quarantine active or contaminated DBs. Verify `PRAGMA integrity_check`, schema version, source/copy hash, row counts, and audit-chain status. Never delete the user's main Call Warden database.
