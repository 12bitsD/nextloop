# nextloop development and verification plan

Current stage: **0.9.0 release verification**. `0.8.0` is published; later milestones are not claimed as implemented.

## Delivery order

```text
0.5.0rc2: Pending Input recovery and guarantee documentation
  → 0.5.0: fault matrix, Store conformance, 10k benchmark, Python 3.14, import contract
  → 0.6: upcasting, operation_id, approval/resume, Compaction source ranges
  → 0.7: opt-in owner leases and persistent Pending behavior
  → 0.8: caller-driven durable Outbox and OTel ID mapping
  → 0.9: reentrant whole-Session deletion and tombstones
  → 1.0: all hard gates and official OpenAI/Anthropic acceptance
```

Each stage requires an independent Standards / Spec / Risk review, complete regression gates, and a separate release. A design or ADR is not accepted evidence until its fault scenarios pass.

## 0.5.0 — falsification and frozen integration paths

- `nextloop.testing` provides Store, ToolRuntime, and DeliveryOwner fault injection, including after-side-effect and after-commit failures.
- The standard Tool + steer + final scenario traverses every append point on Memory and SQLite, before and after commit, and verifies seq, Pending, resource locks, Delivery, and no Tool replay.
- Explicit scenarios cover writer takeover before dispatch, late Tool results, parallel partial completion, Compaction, and repeated recovery.
- `check_event_store()` accepts both built-in Stores and rejects unchecked writers, duplicate event IDs, and incorrect Surface implementations.
- A 10,505-event benchmark checks Memory/SQLite Conversation Projection parity and incremental/whole-history Trace parity while reporting timings.
- CI covers Python 3.11, 3.12, 3.13, and 3.14.
- Existing root re-exports and core/extras module paths are frozen without a physical package split.

See [Architecture and import contract](../design/architecture.md), [Verification matrix](../design/verification-matrix.md), and [CONTRIBUTING](../../CONTRIBUTING.md).

## 0.6 — explicit operation and approval semantics

- Versioned, read-only Event upcasting with real historical fixtures and fail-fast unknown versions.
- `operation_id` is created and persisted with Tool Intent; new model intent receives a new ID.
- `ToolEffect = READ_ONLY | IDEMPOTENT | NON_IDEMPOTENT`, defaulting to NON_IDEMPOTENT. Default Tool-name locking applies only to unresolved uncertain outcomes and is an explicit breaking change.
- Explicit same-ID reconciliation is allowed only for READ_ONLY and IDEMPOTENT operations. It creates a Reconciliation Result notification, never a second Tool Result at the original coordinate.
- Approval distinguishes Tool Intent from dispatch, binds canonical arguments and policy versions, and uses conditional append. Approval precedes side effects; resume uses original IDs and arguments in a new Turn.
- Compaction records source seq ranges and cannot split a Tool Intent from its result.

## 0.7 — opt-in writer leases

- `owner_id` remains optional; existing `attach_writer` behavior stays unchanged when leases are not configured.
- Same-owner restart can take over immediately. Different owners wait for expiry or explicit forced takeover.
- Store time determines expiry; heartbeat renewal is independent. Every actual dispatch retains a writer/lease check.
- SQLite promises same-host multi-process behavior only. Late results from stale writers are discarded and remain uncertain.
- Existing `steer() -> bool` remains compatible; `steer_result()` adds explicit rejection reasons.

## 0.8 — durable, caller-driven Outbox

- Export state and cursors live outside the Event log and do not consume writer epochs.
- `export_pending(session_id=None)` is caller-driven and can enumerate Sessions with unexported closed Turns without starting recovery.
- Watermarks advance only after durable Export acknowledgement. Bounded retries record auditable gaps rather than blocking forever or pretending success.
- Trace replay uses stable OTel 128-bit trace IDs and 64-bit span IDs. No OTel Exporter implementation is included in this milestone.
- Per-Session export locks carry owner tokens and are renewed only during a caller-driven export. Renewal and release reject stale owners; expired locks support crash takeover and old schemas migrate under a write transaction.

## 0.9 — whole-Session deletion

- Deletion first commits a deleting tombstone and fences writers, then deletes primary and derived local state. Interrupted deletion is reentrant.
- A Session-ID-only tombstone prevents recreation. Event-level erasure and external blob reference counting are excluded.
- SQLite verification scans DB, WAL, SHM, and rollback-journal bytes with synthetic canaries. Third-party exports and user backups are not covered.

## 1.0 hard gates

- All Store, ToolRuntime, DeliveryOwner, negative-conformance, takeover, and recovery scenarios pass.
- The 10k benchmark and parity checks pass.
- Python 3.14 CI passes.
- Official OpenAI and Anthropic API acceptance passes. Missing official credentials blocks 1.0; DeepSeek is not a substitute.
- Documentation, ADRs, migration notes, guarantee tables, and actual behavior agree.

## Explicit non-goals through 1.0

No streaming `async for` API, OTel Exporter implementation, remote MCP/OAuth, additional Providers, `@tool` decorator, agent-as-Tool framework, Postgres implementation, physical core/extras split, automatic replay, in-place same-Turn resume, event-level erasure, or external blob lifecycle.
