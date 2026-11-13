# Guarantees and non-guarantees

This page lists the main runtime guarantees and their limits. Use the linked design documents for details and the verification matrix for test evidence.

| Area | nextloop guarantees | Not guaranteed |
|---|---|---|
| Accepted input | A non-empty `input_id` deduplicates accepted input. You can query Pending Input with `AgentLoop.pending_inputs`. Identical retries deduplicate; conflicting content is rejected without consuming the original. `steer_result` explains rejections. | Deduplication when a retry has a different or missing ID. |
| Writer lease | Optional owner and expiry checks, heartbeats, same-owner restart, and takeover after expiry or with force. Same-host contention is verified. Stores without an owner keep legacy behavior. | Distributed locks, cross-host leases, or writes after a stale writer check. |
| Delivery | Delivery is at least once. The DeliveryOwner deduplicates with the stable `final_message_id`. | Exactly-once delivery, or safe delivery without an idempotent owner. |
| Tool execution | A dispatched Tool is never automatically replayed. Explicit reconciliation is limited to `READ_ONLY` and `IDEMPOTENT` operations. | Protection against a model repeating a call, or against duplicate effects from non-idempotent Tools. |
| Resource locks | Uncertain results retain declared resources. Without declared resources, `NON_IDEMPOTENT` uses the Tool name. Successful reconciliation releases the operation's locks. | Global deduplication across operations or resources, or safe concurrent execution of non-idempotent calls. |
| Approval | If any item needs approval, the whole batch stops before dispatch. Resume requires explicit decisions and rechecks IDs, arguments, expiry, and prior dispatch under the Session lock. | Automatic approval, execution before a durable grant, or replay after dispatch without recovery. |
| Event compatibility | Schema 1.0 Events are upcast during reads. Original payload bytes remain available for idempotent append. | Reading Schema 1.1 with 0.5.0 or earlier, or accepting unknown future schemas. |
| Recovery | Recovery closes interrupted facts. The caller starts a new Turn with projected messages and Pending Input. | Replaying the interrupted Turn in place or automatically running uncertain calls. |
| Preview | Preview is temporary and cannot execute partial Tool arguments. | Preview as a durable fact or a replacement for committed Delivery. |
| Observability | Committed Events are authoritative. The live queue is best-effort. `SQLiteOutbox` replays closed Turns with durable cursors, bounded attempts, gaps, and owner-token locks. | Exactly-once export, export of active Turns, or deletion of third-party copies. |
| Session deletion | Idempotent tombstones block reads and writers. Local Event, Trace, and Outbox state is removed. SQLite uses secure delete, checkpoints, and VACUUM for local byte cleanup. | Event-level erasure, rollback of external Tools, or deletion of caller copies, third-party exports, backups, and external assets. |
| MCP | stdio lifecycle, paginated static catalogs, operation metadata, and uncertain crash handling. | Remote MCP, OAuth, dynamic `listChanged`, or non-text content mapping. |

## Release boundaries

- nika integration has its own acceptance process.
- Writer leases work only between processes on the same host.
- Event-level erasure, external-asset lifecycle, backup/GC, and deletion of third-party copies are outside this release.
- DeepSeek is the accepted live Provider gate under ADR-0011. Official OpenAI and Anthropic API acceptance is not claimed. Their recorded Adapter streams are separate evidence.

## Details and evidence

- [Loop interface and ownership](loop-interface.md)
- [Architecture and invariants](architecture.md)
- [Harness and extension interface](harness.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0011 Live Provider acceptance](../adr/0011-live-provider-acceptance.md)
