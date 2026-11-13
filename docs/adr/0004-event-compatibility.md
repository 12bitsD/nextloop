# ADR-0004: Event compatibility and read-only upcasting

Status: accepted

## Context

nextloop 0.5.0 writes Event Schema 1.0. Version 0.6 adds operation identity, approval, and Compaction lineage fields without changing the Event envelope. Compatibility must not rewrite history, accept unknown future versions, or confuse upcast data with original stored bytes.

## Decision

New Events use Schema 1.1. Stores read 1.0 history and upcast it only during loading. Unknown versions fail with `InvariantViolation`. Readers that only support 1.0 cannot read a 1.1 Session.

Rules:

- Upcasting is pure, deterministic, and idempotent. Loading never runs an UPDATE or rewrites Events.
- Idempotent append compares the original stored payload and Event ID, not the upcast representation. Different original content is rejected.
- A 1.0 `TOOL_CALL` without `operation_id` gets a deterministic ID from `session_id`, `turn`, `step`, and `call_id`. The matching call in `ASSISTANT_MESSAGE` gets the same ID. The same call ID in another Step gets a different ID.
- A 1.0 `COMPACTION` without lineage gets `source_seqs=None`. No source range is invented.
- Projection, Trace, and Recovery use upcast Events. Idempotent append uses the stored, non-upcast representation.
- The reserved payload `{"_nextloop_erased": True}` marks erased content for future retention work. Reusing that Event ID raises `PayloadErasedError` without comparing content. No low-entropy PII hash is retained, and Event-level erasure is outside 0.6.
- Each released schema has a real Store fixture from that release. The 1.0 fixture covers repeated call IDs, Compaction, and an interrupted Tool Call. Reading and upcasting must not change fixture bytes.

## Consequences

- Old 1.0 logs remain readable without a migration write or downtime.
- Versions 0.5.0 and earlier intentionally cannot read 1.1 logs.
- Read-time compatibility does not weaken append idempotency or future-version checks.
- Legacy operation IDs are deterministic and coordinate-scoped. New operation IDs introduced in 0.6 are persisted facts, not regenerated during recovery.
- Erased payloads fail closed by identity without implementing deletion early or retaining PII hashes.
