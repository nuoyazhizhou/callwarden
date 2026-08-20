# Executor lease cleanup and orphan recovery

## Lease cleanup on stale holder

- target: `rust_ext/src/daemon/task_collab.rs`
- Treat an active lease whose registered holder is missing, inactive, or beyond the existing orphan heartbeat threshold as recoverable by the protected recovery path; never reclaim a fresh heartbeat or an unexpired live holder.
- Append an immutable lease expiration/recovery audit event in the same transaction before allowing a replacement claim.

## Protected recovery routing

- target: `rust_ext/src/daemon/dispatch.rs`
- Verify `task.claim.recover` remains protected and cannot be invoked by an Executor or via public unguarded fallback.

## Regression tests

- target: embedded Rust tests in `rust_ext/src/daemon/task_collab.rs`
- Cover fresh active holder rejection, stale/missing holder recovery, idempotent recovery, replacement claim, and historical task/evidence immutability.

Forbidden: direct SQLite edits, history rewrite, local fallback, broad capture, unrelated files, apply/close.
