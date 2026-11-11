# nextloop feature worklist

Purpose: distinguish implemented capabilities from remaining work. Executable evidence is owned by the [verification matrix](../design/verification-matrix.md).

## Implemented in the current 0.9 line

| Feature | Implementation | Evidence |
|---|---|---|
| Turn/Step driver | 4D coordinates, append-only Events, thin orchestration | Text, Tool, multi-Step, cancellation, final-order, and budget tests |
| Response protocol | Bounded correction, terminal failures, exact-byte retry | Empty, truncated, malformed, exhaustion, retry, and partial-output tests |
| Tool scheduling | Whole-batch Schema validation, bounded parallel calls, exclusive barriers | Zero-side-effect rejection, concurrency gates, ordering, and truncation tests |
| Recovery | Crash closers, explicit uncertainty, resource locks, delivery-only retry | Every-append fault matrix, real process exits, takeover, late-result, and repeated-recovery tests |
| Pending Input | Log projection by `input_id`, query API, Step-boundary consumption | Accepted/not-consumed windows, duplicate recovery, retry, cancellation, and next-Turn tests |
| Context | Append-only projection, WorkingContext, Compaction, result preview/page/full | Projection parity, compaction, UTF-8 paging, read-back, and capacity tests |
| EventStore | Memory and SQLite, atomic conditional append, idempotency, writer checks | Public conformance plus deliberately broken Store rejection |
| Event compatibility | Read-only 1.0→1.1 upcasting, raw append comparison | Real 1.0/1.1 SQLite fixtures and unknown-schema negative paths |
| Owner leases | Optional owner/epoch/expiry, heartbeat, forced takeover, steer_result | Same/different owner, renewal failure, concurrent resume, late results, and spawn-based multiprocess contention |
| Operation identity | operation_id, ToolEffect, resource locks, explicit Reconciliation | End-to-end propagation, dynamic locks, repeated/concurrent retry, and no-replay evidence |
| Approval | Atomic Intent/request, new-Turn resume, origin pairing, lazy expiry | Zero pre-grant dispatch, crash windows, concurrent resume, rejection, cancellation, and fresh budgets |
| Providers | OpenAI-compatible and Anthropic streaming adapters | Recorded protocol streams plus separately identified live-provider smoke |
| Observability | Model/Tool/Turn Trace DTOs, async Exporters, history | Live/history parity, failure isolation, bounded close, and 10k incremental parity |
| Durable Outbox | Caller-driven replay, persistent cursor/attempts/gaps, owner-token locks and heartbeat | Ack/crash windows, retry exhaustion, lock ownership/timeout/migration, closed-Turn filtering, and no resurrection after deletion |
| Session deletion | Reentrant deleting/deleted tombstones, writer fencing, local Trace/Outbox cleanup, SQLite byte purge | Crash/interruption windows, stale writers, read snapshots, queue isolation, raw DB/WAL/SHM/journal canaries |
| Integrations | Harness, directory Skills, stdio workers, MCP stdio | Offline diagnostics, subprocess conformance, lifecycle, and clean-wheel examples |
| Release contract | Frozen root/module re-exports, Python 3.11–3.14 CI | Import snapshot, complete gates, wheel/sdist, and clean-install checks |

## Remaining work

| Feature | Target and boundary |
|---|---|
| Official OpenAI/Anthropic acceptance | 1.0 hard gate; recorded streams and DeepSeek do not substitute |
| nika product integration | Separate consumer acceptance; no Feishu/Wiki dependency enters nextloop |
| Large external-asset lifecycle | Not claimed; current large Tool Results remain in the EventStore |
| Remote MCP/OAuth and more Providers | Explicitly outside the 1.0 plan |
