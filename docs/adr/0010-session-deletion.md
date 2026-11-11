# ADR-0010: Reentrant whole-Session deletion

Status: accepted

## Context

Event history, writer leases, Trace projection state, live Exporter queues, and Outbox cursors can all retain Session-derived data. Deleting only Event rows neither fences stale writers nor removes local derived copies. Whole-Session deletion is required; event-level erasure remains outside this milestone.

## Decision

- `EventStore.delete_session(session_id)` is idempotent and uses a Session-ID-only tombstone with `deleting` and `deleted` states. The first transaction commits `deleting`, increments any existing writer epoch, and removes lease ownership. The second removes Events and writer state and commits `deleted`.
- A tombstone in either state fails closed for attach, append, writer checks/renewal, surface reads, and recovery reads. Tombstoned Sessions are excluded from `session_ids()` and cannot be recreated through the EventStore API.
- InMemoryEventStore applies the same tombstone and cache-removal semantics without claiming physical destruction of caller-owned copies or Python runtime history.
- SQLite creates the tombstone table lazily on first deletion, preserving read-only historical fixture bytes. Surface and recovery reads use one consistent snapshot for tombstone validation and Event selection.
- SQLite enables `secure_delete` before row deletion. After logical deletion it checkpoints and truncates the WAL, vacuums the database, and checkpoints again. A busy checkpoint or failed physical cleanup surfaces an error; repeating deletion retries cleanup while the tombstone continues to fail closed.
- ObservingEventStore serializes append/export activity with deletion. It removes TraceProjector state, queued records for the deleted Session, and seed markers, and suppresses a record already fetched by the worker. No deleted-Session Trace is newly exported after deletion returns.
- SQLiteOutbox.delete_session remains the owner of Outbox cursor, attempts, gaps, and locks and applies the same SQLite physical cleanup. Every attempt, cursor, and gap write validates the current owner token in its write transaction, so an in-flight export cannot recreate deleted rows. Whole-Session retention calls both idempotent APIs; no cross-database transaction is claimed.
- Tests inject deleting/deleted intermediate states and cleanup crashes, verify stale-writer and read rejection, preserve an unrelated Session, and scan DB, WAL, SHM, and rollback-journal bytes for synthetic content canaries.

## Boundaries

Deletion does not roll back an already dispatched external Tool effect. Callers must quiesce active Loops and Exporters when they require physical destruction without late side effects. Caller-owned Event/Trace copies, third-party exports, backups, offline replicas, external assets, and event-level erasure are outside this guarantee.

## Consequences

- A crash at any point leaves either a fail-closed deleting/deleted tombstone or a completed deletion; repeating both Store-specific cleanup APIs resumes the remaining work.
- Only the Session ID and deletion state remain in the EventStore. No Event payload or Compaction summary is retained in the tombstone.
- Third-party EventStores and decorators must implement or delegate `delete_session()` and preserve fail-closed tombstone behavior to satisfy the updated public contract.
