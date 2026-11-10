# ADR-0004: Event compatibility and read-only upcasting

Status: accepted

## Context

nextloop 0.5.0 writes Event schema 1.0. The 0.6 operation identity, approval, and Compaction lineage work adds payload fields without changing the Event envelope. Compatibility must not rewrite history, silently accept a future schema, or compare upcast payloads as if they were original bytes.

## Decision

New Events are written as schema 1.1. Stores accept 1.0 history and upcast it only while loading. Unknown schema versions fail closed with `InvariantViolation`; older readers that only understand 1.0 therefore cannot read a 1.1 Session.

- Upcasting is a pure, deterministic, idempotent function. No Store UPDATE or rewrite is part of loading.
- The original stored payload and event ID remain authoritative for idempotent append comparison. Comparing a draft with an upcast representation is not equivalent to comparing original bytes and is rejected when they differ.
- A schema-1.0 TOOL_CALL without `operation_id` receives a deterministic ID derived from `session_id`, `turn`, `step`, and `call_id`. The same coordinates in ASSISTANT_MESSAGE Tool Calls receive the same ID; identical call IDs in different Steps receive different IDs.
- A schema-1.0 COMPACTION without lineage receives `source_seqs=None`. No exact source range is fabricated.
- Projection, Trace, and Recovery consume upcast Events; idempotent append uses the stored, non-upcast representation.
- The reserved single-key payload `{"_nextloop_erased": True}` denotes erased content for future retention work. Reusing that event ID raises `PayloadErasedError` without comparing payload content. No content hash is retained for low-entropy PII, and event-level erasure itself is outside 0.6.
- Every released schema has a real Store fixture produced by that release. The 1.0 fixture contains normal multi-Step Tool Calls with a repeated call ID, Compaction, and an interrupted Tool Call. New readers must preserve raw fixture bytes while loading and upcasting.

## Consequences

- Old 1.0 logs remain readable without a migration write or downtime.
- New 1.1 logs are intentionally not readable by 0.5.0 and earlier.
- Read-time compatibility does not weaken append idempotency or future-version checks.
- Operation identity for legacy calls is deterministic and coordinate-scoped. New operation IDs introduced in 0.6 are persisted facts, not regenerated during recovery.
- Payload erasure has a fail-closed identity rule without prematurely implementing deletion or retaining PII hashes.
