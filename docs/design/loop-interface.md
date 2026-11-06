# Loop interface and ownership

Status: **v1-draft accepted design**, not an implemented or released API.

## Independence and version dependency

This document and `src/nextloop/contracts.py` are the only integration dependency between nextloop and its consumers. Public API compatibility (`API_VERSION`) and event-schema compatibility (`EVENT_SCHEMA_VERSION`) are versioned independently.

## Public shape

```text
AgentLoop.run(TurnContext) -> TurnOutcome
AgentLoop.steer(session_id, content, *, input_id?)
AgentLoop.cancel(session_id, cause?)
AgentLoop.recover(session_id, store, tools, delivery) -> RecoveryOutcome
```

All public types, `ToolOutcome` variants, event envelopes, Adapter Protocols, and errors are defined in `src/nextloop/contracts.py` ([ADR-0003](../adr/0003-python-and-centralized-contracts.md)).

### Coordinate system

Every execution fact is indexed by 4D integer coordinates plus a writer fence:

- `session_id: str`
- `seq: int` (1-based per Session)
- `turn: int` (1-based per Session)
- `step: int` (1-based per Turn; represents one logical model call + Tool batch)
- `attempt: int` (1-based per Step; Provider retry counter)
- `writer_epoch: int` (monotonic fencing token per Session writer attachment)

Separate `run_id` and `model_call_id` UUIDs are omitted.

## Adapter seams

### EventStore

- Appends batches of events atomically with `(session_id, expected_seq, writer_epoch)` verification and `event_id` idempotency.
- Supports `surface_op` (`append | epoch | none`) indexing so ContextBuilder can load only events from the latest `epoch` watermark (`seq >= last_epoch_seq AND surface_op != 'none'`).
- Treats sequence and `writer_epoch` conflicts as fatal crash/split-brain invariants; normal in-process `steer` vs. `final` ordering is serialized by the in-memory `SessionDriver` lock.

### ModelProvider

- Configured externally by the consumer (`openai` or `anthropic` protocol, `model_id`, `base_url`, credentials, request defaults).
- Streams one frozen `CandidateRequest` for `(turn, step, attempt)` and returns a logical `ModelResponse` or classified `ProviderError`.
- Never persists connection configuration into Trajectory, never executes Tools, and never runs a hidden retry loop.

### ContextBuilder & Compactor

- `ContextBuilder` projects Conversation from the latest `epoch` watermark + subsequent `append` surface events, keeping prior `Message` objects frozen in memory.
- Applies hash deduplication and step damping to `WorkingContext` updates, and folds only the latest active `WorkingContext` immediately after a `compaction` epoch event.
- `Compactor` runs a Tool-less summary call when preflight hits the 75% input threshold, `I + O` capacity limit, or `/compact`, committing one `surface_op = 'epoch'` event.

### ToolRuntime

- Supplies the `ToolSpec` catalog, validates whole batches before any dispatch, and classifies calls as `parallel` or `exclusive` (`max_parallel = 4` default).
- Executes admitted calls and returns one of the **5 closed `ToolOutcome` ADT variants**:
  - `Completed(content, is_error, structured, assets)`
  - `Skipped(reason)`
  - `Cancelled(settled, partial_content, resource_locks)`
  - `UncertainSettled(content, assets)`
  - `UncertainHanging(content, resource_locks)`

### DeliveryOwner

- Delivers a committed `final_message_id` after `final/message + turn/end` are durable.
- Retries only external delivery on recovery; never reruns the model or Tools.

## Canonical documents

- [DSH-aligned AgentLoop](agent-loop.md)
- [ReAct execution protocol](react-protocol.md)
- [ContextBuilder](context-builder.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)
- [ADR-0003](../adr/0003-python-and-centralized-contracts.md)
