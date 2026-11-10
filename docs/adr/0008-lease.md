# ADR-0008: Optional owner leases and writer-scoped input

Status: accepted

## Context

Writer epochs reject stale appends but do not express ownership or expiry. A paused process can resume after another instance takes over, and inactive processes currently cannot describe why steer or cancel was rejected. Lease behavior must be optional so existing single-writer deployments do not migrate.

## Decision

- `owner_id` is opt-in. Without it, `attach_writer` keeps its existing unconditional epoch increment and writer checks keep their existing semantics.
- Lease-enabled Stores bind `session_id`, `owner_id`, `writer_epoch`, and `expires_at`. The Store clock decides expiry. Same-owner restart may take over immediately; a different owner waits for expiry or explicit `force=True` takeover. Every takeover increments writer_epoch.
- `check_writer` and append validate owner, epoch, and expiry for lease-enabled Stores. `renew_writer` extends expiry without changing epoch and cannot revive an expired lease.
- AgentLoop heartbeat renews independently of event appends, including during long Tools. Heartbeat proves only that the process is scheduled; forced takeover remains available for a stuck main loop. SQLite promises same-host, multi-process behavior only, not distributed locking.
- Every Tool dispatch keeps a writer check after TOOL_CALL. An expired writer fails its next append and cannot dispatch new Tools. A late external result that cannot be committed is discarded and remains uncertain.
- `steer() -> bool` remains. `steer_result()` additionally returns `accepted`, `session_missing`, `not_writer`, `turn_closed`, `queue_full`, `invalid_input`, or `approval_pending`. Non-writers never append INPUT_ACCEPTED or cancellation facts.
- Pending Input remains a pure log projection. The in-memory inbox is bounded by `max_pending_inputs`, while Pending remains recoverable from the log after process restart.

## Consequences

- Existing deployments keep legacy behavior unless they configure an owner and lease duration.
- Ownership, expiry, renewal, and forced takeover are durable facts rather than process-local assumptions.
- Callers receive machine-readable input rejection reasons without a second input authority.
- Multi-process input acceptance is limited to the current valid writer; cross-host lease coordination is explicitly excluded.
