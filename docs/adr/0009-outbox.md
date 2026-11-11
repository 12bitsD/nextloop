# ADR-0009: Caller-driven durable Outbox

Status: accepted

## Context

The existing Exporter queue is in memory and can lose traces on crash. A durable export must not own writer epochs, require a running AgentLoop, or report active Turns as complete.

## Decision

- `SQLiteOutbox` owns export cursors, attempt counters, and gaps in a separate table. These records never advance `writer_epoch` and are not an Event authority.
- `export_pending(session_id=None)` is caller-driven and has no resident worker. It can enumerate all EventStore Sessions and continue exporting a closed Turn after process restart without calling `recover()`.
- Only Turns with a committed TURN_END are exported. Model, Tool, and Turn traces come from the same `TraceProjector` used for live/history projection. An active later Turn does not block already closed earlier Turns.
- A trace is eligible only when its source seq exceeds the durable cursor. Successful Export acknowledgement commits the cursor in a separate transaction. A crash after acknowledgement but before that commit can replay; a crash after the commit cannot replay.
- Failed exports persist an attempt counter. `max_attempts` is bounded; reaching it records an auditable export gap and advances to the next exportable point instead of blocking forever or claiming success.
- OTel mapping uses a deterministic 128-bit `trace_id` per Session and deterministic 64-bit `span_id` per trace record. The OTel Exporter itself is outside this milestone.
- Tool duration measures actual execution only. Approval waiting does not enter Tool duration; Turn duration remains separate.
- Each exporting instance writes a unique owner token with its Session lock. A heartbeat runs only during `export_pending()` and renews at one-third of `lock_timeout_seconds`; release and renewal must match that token. Expired locks can be reclaimed after a process crash, and an old owner cannot delete its successor's lock. Old lock tables migrate under a write transaction.

## Third-party EventStore migration

Implementations and decorators must add or delegate `session_ids() -> Sequence[str]`, returning all Sessions with committed Events in stable order. Without this method, caller-driven export cannot enumerate unexported Sessions after restart. No writer ownership or lease behavior is required.

`export_timeout_seconds` bounds each cooperative Exporter call. A timeout follows the same attempt/gap path as other failures; cancelling a Python thread does not forcibly stop its code, and an asynchronous Exporter that suppresses cancellation can delay cleanup. Late side effects remain possible and are not reported as successful acknowledgement. Asynchronous Exporters must respond to cancellation; arbitrary user code is not hard-isolated by the timeout. `delete_session(session_id)` removes only Outbox cursor, attempts, gaps, and locks, with the same SQLite physical cleanup used for Events. Whole-Session Event deletion is specified by ADR-0010. Outbox attempts, cursor advances, and gaps validate the current owner token in their write transaction so deleted state cannot be resurrected by an old export.

## Consequences

- Export is at least once across process crashes and supports durable replay without starting the Loop.
- Cursor acknowledgement and export-gap persistence are separate from Event append transactions.
- Active work is never misrepresented as a complete Trace, and persistent failures remain inspectable.
