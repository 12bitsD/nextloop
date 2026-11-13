# nextloop feature worklist

Status: **capability record for the 1.0 line**, not a current implementation backlog. It separates implemented capabilities from work outside that release. The [verification matrix](../design/verification-matrix.md) owns the executable evidence.

## Implemented capabilities

| Capability | What is included | Evidence |
|---|---|---|
| Turn/Step driver | Coordinates, append-only Events, thin orchestration | Text, Tool, multi-Step, cancellation, final-order, and budget tests |
| Response protocol | Bounded correction, terminal failures, exact-byte retry | Empty, truncated, malformed, exhaustion, retry, and partial-output tests |
| Tool scheduling | Whole-batch validation, bounded parallel calls, exclusive barriers | Zero-side-effect rejection, concurrency, ordering, and truncation tests |
| Recovery | Crash closers, uncertainty, resource locks, delivery-only retry | Append fault matrix, process exits, takeover, late results, and repeated recovery |
| Pending Input | Log Projection by `input_id`, query API, Step-boundary consumption | Acceptance windows, duplicate recovery, retry, cancellation, and next-Turn tests |
| Context | Append-only Projection, WorkingContext, Compaction, result preview/page/full | Projection parity, Compaction, UTF-8 paging, read-back, and capacity tests |
| EventStore | Memory and SQLite, atomic append, idempotency, writer checks | Public conformance and deliberately broken Store rejection |
| Event compatibility | Read-only 1.0→1.1 upcasting and raw append comparison | Real SQLite fixtures and unknown-schema failures |
| Owner leases | Optional owner/epoch/expiry, heartbeat, takeover, `steer_result` | Owner changes, renewal failure, concurrent resume, late results, and multiprocess contention |
| Operation identity | `operation_id`, `ToolEffect`, resource locks, explicit reconciliation | End-to-end propagation, dynamic locks, repeated/concurrent retry, and no replay |
| Approval | Atomic Intent/request, new-Turn resume, origin pairing, lazy expiry | Zero pre-grant dispatch, crash windows, concurrent resume, rejection, cancellation, and fresh budgets |
| Providers | OpenAI-compatible and Anthropic streaming adapters | Recorded protocol streams and separately identified live smoke |
| Observability | Model/Tool/Turn Traces, async Exporters, history | Live/history parity, failure isolation, bounded close, and 10k incremental parity |
| Durable Outbox | Caller-driven replay, durable cursor/attempts/gaps, owner-token locks | Ack/crash windows, retry exhaustion, lock lifecycle, closed-Turn filtering, and deletion safety |
| Session deletion | Reentrant tombstones, writer fencing, local cleanup, SQLite byte purge | Crash windows, stale writers, read snapshots, queue isolation, and raw database canaries |
| Integrations | Harness, directory Skills, stdio workers, MCP stdio | Offline diagnostics, subprocess conformance, lifecycle, and clean-wheel examples |
| Release contract | Frozen re-exports and Python 3.11–3.14 CI | Import snapshots, complete gates, wheel/sdist, and clean installs |

## Outside the 1.0 plan

| Area | Boundary |
|---|---|
| Official OpenAI/Anthropic acceptance | Not claimed; recorded Adapter streams are separate evidence. DeepSeek is the accepted 1.0 live gate under ADR-0011 |
| nika product integration | Separate consumer acceptance. Feishu and Wiki dependencies stay outside nextloop |
| Large external-asset lifecycle | Not claimed. Large Tool Results remain in the EventStore |
| Remote MCP/OAuth and more Providers | Explicitly outside the 1.0 plan |
