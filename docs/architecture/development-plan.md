# nextloop development and verification plan

Status: **release history through 1.0, with a 1.1.0rc1 verification snapshot**. This is not the current product roadmap. See [Project direction](../../README.md#direction) for current direction. `1.0.0` is published; ADR-0011 defines the DeepSeek live Provider gate.

## Delivery order

```text
0.5.0rc2: Pending Input recovery and guarantee documentation
  → 0.5.0: fault matrix, Store conformance, 10k benchmark, Python 3.14, import contract
  → 0.6: upcasting, operation_id, approval/resume, Compaction source ranges
  → 0.7: opt-in owner leases and persistent Pending behavior
  → 0.8: caller-driven durable Outbox and OTel ID mapping
  → 0.9: reentrant whole-Session deletion and tombstones
  → 1.0: all hard gates and maintainer-selected DeepSeek live acceptance
```

Each stage required independent Standards / Spec / Risk reviews, complete regression gates, and a separate release. A design or ADR became evidence only after its fault scenarios passed.

## 0.5.0 — fault injection and public contracts

- `nextloop.testing` injects Store, ToolRuntime, and Delivery failures before and after side effects and commits.
- The standard Tool + steer + final scenario checks every append on Memory and SQLite, including sequence, Pending, resource locks, Delivery, and no Tool replay.
- Other scenarios cover writer takeover, late Tool Results, parallel partial completion, Compaction, and repeated recovery.
- `check_event_store()` accepts the built-in Stores and rejects unchecked writers, duplicate Event IDs, and invalid Surface behavior.
- A 10,505-event benchmark checks Memory/SQLite Conversation parity and incremental/whole-history Trace parity.
- CI covers Python 3.11, 3.12, 3.13, and 3.14.
- Root re-exports and core/extras module paths are frozen without a physical package split.

See [Architecture and import contract](../design/architecture.md), [Verification matrix](../design/verification-matrix.md), and [CONTRIBUTING](../../CONTRIBUTING.md).

## 0.6 — operations and approvals

- Event upcasting is versioned and read-only, with real historical fixtures and fail-fast unknown versions.
- Tool Intent receives a persistent `operation_id`; new model intent receives a new ID.
- `ToolEffect = READ_ONLY | IDEMPOTENT | NON_IDEMPOTENT`, defaulting to `NON_IDEMPOTENT`. Default Tool-name locking applies only to uncertain outcomes.
- Explicit same-ID reconciliation is limited to `READ_ONLY` and `IDEMPOTENT` operations. It creates a Reconciliation Result, not a second Tool Result at the original coordinate.
- Approval binds canonical arguments and policy versions before dispatch. Resume uses the original IDs and arguments in a new Turn.
- Compaction records source sequence ranges and cannot separate a Tool Intent from its result.

## 0.7 — optional writer leases

- `owner_id` is optional; `attach_writer` keeps its legacy behavior without a lease.
- The same owner takes over immediately after restart. Another owner waits for expiry or explicit force.
- The Store clock decides expiry. Heartbeats renew independently, and every dispatch checks the writer and lease.
- SQLite supports same-host process contention only. Late results from stale writers are discarded and remain uncertain.
- `steer() -> bool` remains compatible; `steer_result()` adds structured rejection reasons.

## 0.8 — durable Outbox

- Export state and cursors live outside the Event log and do not consume writer epochs.
- `export_pending(session_id=None)` can enumerate Sessions with unexported closed Turns without starting recovery.
- Watermarks advance only after Export acknowledgement. Bounded retries record auditable gaps instead of blocking forever or claiming success.
- Trace replay uses stable OTel 128-bit trace IDs and 64-bit span IDs. This milestone does not include an OTel Exporter.
- Per-Session locks carry owner tokens and renew only during caller-driven export. Renewal and release reject stale owners, expired locks support takeover, and old schemas migrate under a write transaction.

## 0.9 — whole-Session deletion

- Deletion first writes a tombstone and fences writers, then removes primary and derived local state. Interrupted deletion is safe to repeat.
- A Session-ID-only tombstone prevents recreation. Event-level erasure and external blob reference counting are excluded.
- SQLite verification scans DB, WAL, SHM, and rollback-journal bytes with synthetic canaries. Third-party exports and user backups are not covered.

## 1.0 hard gates

- All Store, ToolRuntime, DeliveryOwner, negative-conformance, takeover, and recovery scenarios pass.
- The 10k benchmark and parity checks pass.
- Python 3.14 CI passes.
- DeepSeek live text, Tool Calling, Preview, usage, Delivery, Outbox, and deletion scenarios pass under ADR-0011. Official OpenAI and Anthropic hosted API acceptance is not claimed and is not a 1.0 blocker.
- Documentation, ADRs, migration notes, guarantees, and actual behavior agree.

## Non-goals through 1.0

No streaming `async for` API, OTel Exporter, remote MCP/OAuth, additional Providers, `@tool` decorator, agent-as-Tool framework, Postgres implementation, physical core/extras split, automatic replay, in-place same-Turn resume, Event-level erasure, or external blob lifecycle.
