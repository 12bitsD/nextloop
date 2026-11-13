# ADR-0009: Caller-driven durable Outbox

Status: accepted

## Context

The in-memory Exporter queue can lose Traces during a crash. Durable export must not change writer epochs, require a running AgentLoop, or mark an active Turn as complete.

## Decision

### Export and cursors

- `SQLiteOutbox` stores cursors, attempt counters, and gaps in separate tables. These records do not advance `writer_epoch` and are not an Event authority.
- `export_pending(session_id=None)` is called by the application and has no resident worker. It can enumerate EventStore Sessions and export closed Turns after restart without calling `recover()`.
- Only Turns with a committed `TURN_END` are exported. Model, Tool, and Turn Traces use the same `TraceProjector` as live and history reads. A later active Turn does not block earlier closed Turns.
- A Trace is eligible only when its source sequence exceeds the durable cursor. Successful Export acknowledgement commits the cursor in a separate transaction. A crash after acknowledgement but before that commit can replay; a crash after the commit cannot.
- Failed exports persist attempts. At `max_attempts`, Outbox records an auditable gap and moves to the next exportable point instead of blocking forever or claiming success.

### Identity, timing, and locks

- OTel mapping uses a deterministic 128-bit `trace_id` per Session and a deterministic 64-bit `span_id` per Trace record. The OTel Exporter itself is outside this milestone.
- Tool duration measures execution only. Approval waiting is excluded; Turn duration is separate.
- Each exporting instance writes a unique owner token with its Session lock. A heartbeat runs only during `export_pending()` and renews at one-third of `lock_timeout_seconds`.
- Renewal and release must match the owner token. Expired locks can be reclaimed after a crash, and an old owner cannot delete its successor's lock. Old lock tables migrate under a write transaction.

## Third-party EventStore migration

Stores and decorators must add or delegate `session_ids() -> Sequence[str]`, returning all Sessions with committed Events in stable order. Outbox needs this method to find unexported Sessions after restart. Writer ownership and leases are not required.

`export_timeout_seconds` limits cooperative Exporter calls. Timeouts follow the same attempt/gap path as other failures, but they do not forcibly stop user code:

- a synchronous Exporter thread may still finish and cause side effects;
- an asynchronous Exporter that ignores cancellation can delay cleanup;
- late side effects are not reported as successful acknowledgement;
- asynchronous Exporters must respond to cancellation.

`delete_session(session_id)` removes only Outbox cursors, attempts, gaps, and locks, using the same SQLite physical cleanup as Events. Whole-Session Event deletion is defined by ADR-0010. Outbox writes validate the current owner token so an old export cannot recreate deleted state.

## Consequences

- Export is at least once across crashes and can replay without starting the Loop.
- Cursor acknowledgement and export-gap persistence are separate from Event append transactions.
- Active work is never reported as complete, and persistent export failures remain inspectable.
