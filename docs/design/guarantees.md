# Guarantees and non-guarantees

This page summarizes the public runtime boundaries. The linked design documents and verification matrix define the detailed semantics and their executable evidence.

| Area | Guarantee | Not a guarantee |
|---|---|---|
| Accepted input | Accepted inputs are projected from the log by `input_id` and can be queried with `AgentLoop.pending_inputs`. Explicit IDs are non-empty; identical retries deduplicate, conflicting content is rejected without consuming the original. `steer_result` exposes structured rejection reasons. | Automatic deduplication when callers retry with a different or missing ID. |
| Writer lease | Optional owner/epoch/expiry checks with heartbeat, same-owner restart, expiry or forced takeover, and spawn-verified same-host contention. Legacy behavior remains when no owner is configured. | Distributed locks, cross-host leases, or execution after an expired/stale writer check. |
| Delivery | Delivery is at least once; DeliveryOwner must deduplicate by stable `final_message_id`. | Exactly-once delivery or delivery without an idempotent owner. |
| Tool execution | The library never automatically replays an already dispatched Tool. Explicit reconciliation is limited to READ_ONLY and IDEMPOTENT operations. | No duplicate external effect when a model reissues a call or a Tool is not idempotent. |
| Resource locks | Unresolved uncertain results retain declared resources; otherwise NON_IDEMPOTENT uses the Tool name. Successful reconciliation releases all locks associated with the operation. | Global idempotency across different resources or operations, or safe concurrent execution of NON_IDEMPOTENT calls. |
| Approval | Any approval-required item suspends the whole batch before dispatch. Resume requires explicit decisions, binds original IDs/arguments, and rechecks expiry and prior dispatch inside the Session lock. | Automatic approval, execution before a durable grant, or replay after dispatch without recovery. |
| Event compatibility | Schema 1.0 is upcast only during reads; original payload bytes are preserved for idempotent append. | Reading schema 1.1 with 0.5.0 or earlier, or accepting unknown future schemas. |
| Recovery | Recovery writes crash closers; the caller starts a new Turn with projected prior messages and Pending Input. | In-place replay of the interrupted Turn or automatic execution of uncertain calls. |
| Preview | Preview is transient and cannot execute partial Tool arguments. | Preview as a durable EventStore fact or a substitute for committed Delivery. |
| Observability | Committed Events are authoritative. The live queue is best-effort; `SQLiteOutbox` durably replays closed Turns with cursors, bounded attempts, gaps, and owner-token locks. | Exactly-once export, exporting active Turns, or cleanup of third-party exported copies. |
| Session deletion | Idempotent deleting/deleted tombstones fence writers and reads; local Events, Trace state, and Outbox rows are removed, with SQLite secure-delete/checkpoint/VACUUM byte cleanup. | Event-level erasure, external Tool rollback, or deletion of caller copies, third-party exports, backups, and external assets. |
| MCP | stdio lifecycle, paginated static catalogs, operation metadata, and uncertain crash handling. | Remote MCP, OAuth, dynamic `listChanged`, or non-text content mapping. |

## Release boundaries

- nika integration is separate acceptance work.
- Writer leases coordinate same-host processes only; distributed locks and exactly-once export are not provided.
- Event-level erasure, external-asset lifecycle, backup/GC, and deletion of third-party copies are not part of this release.
- DeepSeek is the accepted live Provider gate under ADR-0011. Official OpenAI and Anthropic API acceptance is not claimed; their recorded Adapter streams are separate evidence.

## Details and evidence

- [Loop interface and ownership](loop-interface.md)
- [Architecture and invariants](architecture.md)
- [Harness and extension interface](harness.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0011 Live Provider acceptance](../adr/0011-live-provider-acceptance.md)
