# G0 Experiment Skill Handoff v1

## Delivered package

The repository skill is `skills/g0-experiment/`. Its entry point is `SKILL.md`; detailed material is in `references/`.

The canonical protocol is `docs/design/g0-experiment-protocol-v1.md`. Existing batch-specific runbooks and prompts remain historical or compatibility documents; new work should start from the canonical protocol and this skill.

## Role usage

- Recovery Agent: read the Recovery section and evidence contract first.
- Batch Creator: require a Recovery pass or trusted current evidence, then run the Creator checklist.
- Independent Reviewer: validate the batch-specific binding before reading any sample.
- Coordinator: use the state machine and never advance a role from prose alone.

## Versioning

Treat the protocol version, skill version, and experiment protocol fingerprint as bound inputs. A new protocol must create a new versioned skill/spec and must not silently reinterpret old batches.

## Non-product boundary

G0 evidence remains non-product evidence. It cannot automatically close tasks, enable P1, or replace product CI and release verification.
