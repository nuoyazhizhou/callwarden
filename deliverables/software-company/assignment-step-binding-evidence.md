# Assignment step/task binding evidence

- Remediation task: `T-1788078550140-bb9d6d44`
- Source blocked task: `T-1788077285594-4eceeaac`
- Scope: daemon assignment projection and current-assignment selection only.

## Implementation

Assignment events are now required to carry the same `task_id` in their
immutable payload as the enclosing `task_events` row. In an authority schema,
an assignment with a `step_id` not present in `task_steps` for that task is
excluded from the current projection. Historical malformed events are not
edited or deleted.

## Verification

Focused Rust command:

```text
cargo test --manifest-path rust_ext/Cargo.toml assignment_queue --lib
8 passed; 0 failed
```

The focused tests cover normal replay/takeover/heartbeat/event ordering plus:

- cross-task assignment payload rejection;
- rejection of a step bound to another task.

Live daemon read-only assignment projections were also checked. The current
assignment for `T-1788077285594-4eceeaac` is task-bound to
`S-1788077285599-4f1e48e4`; the current assignment for
`T-1788078550140-bb9d6d44` is task-bound to
`S-1788078550142-bbb5f170`. No cross-task step appeared in either projection.

`cw daemon ping` reported PID `13488`, authority/task DB fingerprint
`eb94a3d750a4037a7e4fd9058fd858ff76c3e8410fa0e79939577456d8c44458`, and the
live runtime binary SHA-256 was
`90d657cc4198dcf3b76895e17408410916a1ec5695842baa61380334bd4503c4`.

No source task verdict, reviewer state, historical evidence, apply, or close
was modified.
