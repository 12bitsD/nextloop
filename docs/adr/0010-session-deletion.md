# ADR-0010: Reentrant whole-Session deletion

Status: accepted

## Context

Session data can remain in Event history, writer leases, Trace state, live Exporter queues, and Outbox cursors. Deleting only Event rows leaves stale writers and derived copies behind. This milestone covers whole-Session deletion, not Event-level erasure.

## Decision

### Tombstones and deletion order

`EventStore.delete_session(session_id)` is idempotent and uses a Session-ID-only tombstone with `deleting` and `deleted` states.

1. Commit `deleting`, increment any existing writer epoch, and remove lease ownership.
2. Remove Events and writer state, then commit `deleted`.

Either state fails closed for attach, append, writer checks and renewal, Surface reads, and recovery reads. Tombstoned Sessions are excluded from `session_ids()` and cannot be recreated through the EventStore API.

### Store behavior

- `InMemoryEventStore` applies the same tombstone and cache-removal rules. It does not claim to destroy caller-owned copies or Python runtime history.
- SQLite creates the tombstone table only on first deletion, so opening historical fixtures for reads does not rewrite them. Tombstone validation and Event selection use one consistent snapshot.
- SQLite enables `secure_delete` before deleting rows. After logical deletion, it checkpoints and truncates the WAL, vacuums the database, and checkpoints again. Cleanup failures are reported and safe to retry; the tombstone continues to fail closed.

### Exporters and Outbox

`ObservingEventStore` serializes append/export activity with deletion. It removes TraceProjector state, queued records for the Session, and seed markers. A record already fetched by the worker is suppressed. No new Trace for the deleted Session is exported after deletion returns.

`SQLiteOutbox.delete_session` owns Outbox cursors, attempts, gaps, and locks and applies the same SQLite cleanup. Every attempt, cursor, and gap write validates the current owner token, so an active export cannot recreate deleted rows.

Whole-Session cleanup calls both idempotent APIs. No cross-database transaction is claimed.

### Verification

Tests inject intermediate deletion states and cleanup crashes. They verify stale-writer and read rejection, preserve an unrelated Session, and scan DB, WAL, SHM, and rollback-journal bytes for synthetic canaries.

## Boundaries

Deletion does not roll back an already dispatched external Tool effect. Callers must stop active Loops and Exporters when they require physical destruction without late side effects.

Caller-owned Event/Trace copies, third-party exports, backups, offline replicas, external assets, and Event-level erasure are outside this guarantee.

## Consequences

- A crash leaves either a fail-closed deleting/deleted tombstone or a completed deletion. Repeating both Store-specific cleanup APIs resumes the remaining work.
- Only the Session ID and deletion state remain in the EventStore. Tombstones contain no Event payload or Compaction summary.
- Third-party EventStores and decorators must implement or delegate `delete_session()` and preserve fail-closed tombstone behavior.
