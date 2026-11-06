# Loop interface and ownership

Status: **v1-draft accepted design**, with public contracts implemented in `src/nextloop/contracts.py`.

## Independence and version dependency

This document and `src/nextloop/contracts.py` are the only integration dependency between nextloop and its consumers. Public API compatibility (`API_VERSION`) and event-schema compatibility (`EVENT_SCHEMA_VERSION`) are versioned independently.

## Public shape

```text
AgentLoop.run(TurnContext) -> TurnOutcome
AgentLoop.steer(session_id, content, *, input_id?)
AgentLoop.cancel(session_id, cause?)
AgentLoop.recover(session_id, store, tools, delivery) -> RecoveryOutcome
```

### Coordinate system

Every execution fact is indexed by 4D integer coordinates plus a writer fence:

- `session_id: str`
- `seq: int` (1-based per Session)
- `turn: int` (1-based per Session)
- `step: int` (1-based per Turn; represents one logical model call + Tool batch)
- `attempt: int` (1-based per Step; Provider retry counter)
- `writer_epoch: int` (monotonic fencing token per Session writer attachment)

## The Only 4 Adapter Seams

Context assembly (`surface_op` watermark projection) and Compaction (calling `ModelProvider` with `LoopConfig.compaction_prompt` and Tools disabled) are built-in `nextloop` mechanisms configured via `LoopConfig`. Only four real Adapter Protocols exist:

### 1. EventStore

- `attach_writer(session_id) -> (writer_epoch, last_seq, last_turn)`
- `append(session_id, writer_epoch, expected_seq, events) -> Sequence[Event]`
- `load_surface(session_id) -> Sequence[Event]` (O(Δ) query from latest `SurfaceOp.EPOCH` watermark where `surface_op != NONE`)
- `load_tail_for_recovery(session_id) -> Sequence[Event]`

### 2. ModelProvider

- `complete(request, *, attempt, cancel) -> ModelResponse`
- Configured externally (`openai` or `anthropic` protocol, `model_id`, `base_url`, credentials). Never persists connection configuration into Trajectory, never executes Tools, and never runs a hidden retry loop.

### 3. ToolRuntime

- `catalog() -> Sequence[ToolSpec]` (each spec declares `mode = PARALLEL | EXCLUSIVE`)
- `execute(call, arguments, *, session_id, turn, step, cancel) -> ToolOutcome`
- Returns one of the **5 closed `ToolOutcome` ADT variants**:
  - `Completed(content, is_error=False, concludes_turn=False, structured=None, assets=())` — `concludes_turn=True` (e.g. `ask_user`) delivers `content` as the Turn's final reply and ends the Turn cleanly without an in-memory `WaitingRequest` state.
  - `Skipped(reason)`
  - `Cancelled(settled, partial_content="", resource_locks=())`
  - `UncertainSettled(content, assets=())`
  - `UncertainHanging(content, resource_locks=())`

### 4. DeliveryOwner

- `deliver(message: FinalMessage) -> None`
- Delivers a committed `final_message_id` after `final/message + turn/end` are durable. Retries only external delivery on recovery.

## Canonical documents

- [DSH-aligned AgentLoop](agent-loop.md)
- [ReAct execution protocol](react-protocol.md)
- [ContextBuilder](context-builder.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)
- [ADR-0003](../adr/0003-python-and-centralized-contracts.md)
