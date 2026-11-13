# ADR-0008: Optional owner leases and writer-scoped input

Status: accepted

## Context

Writer epochs reject stale appends but do not describe ownership or expiry. A paused process can resume after another instance takes over. Callers also need to know why steer or cancel was rejected. Leases must remain optional so existing single-writer deployments do not need to migrate.

## Decision

- `owner_id` is optional. Without it, `attach_writer` and writer checks keep their legacy behavior.
- Lease-enabled Stores bind `session_id`, `owner_id`, `writer_epoch`, and `expires_at`. The Store clock decides expiry. The same owner can take over immediately after restart; another owner waits for expiry or uses `force=True`. Every takeover increments `writer_epoch`.
- `check_writer` and append validate owner, epoch, and expiry. `renew_writer` extends expiry without changing the epoch and cannot revive an expired lease.
- AgentLoop heartbeats renew independently of Event appends, including during long Tools. A heartbeat proves only that the process was scheduled. A stuck main loop can still be taken over with force.
- SQLite supports same-host process contention only, not distributed locking.
- Every Tool dispatch keeps the writer check after `TOOL_CALL`. An expired writer cannot append or dispatch more Tools. A late result that cannot be committed is discarded and remains uncertain.
- `steer() -> bool` remains. `steer_result()` also returns `accepted`, `session_missing`, `not_writer`, `turn_closed`, `queue_full`, `invalid_input`, or `approval_pending`. Non-writers never append input or cancellation facts.
- Pending Input still comes from the log. `max_pending_inputs` limits only the in-memory Inbox; durable Pending remains recoverable after restart.

## Consequences

- Existing deployments keep legacy behavior unless they configure an owner and lease duration.
- Ownership, expiry, renewal, and takeover are durable facts, not process-local assumptions.
- Callers receive machine-readable rejection reasons without another input authority.
- Only the current valid writer accepts multi-process input. Cross-host coordination is excluded.
