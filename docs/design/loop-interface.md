# Loop interface and ownership

Status: **v1-draft implemented for 0.2.0rc1**, with public contracts in `src/nextloop/contracts.py`.

## Independence and version dependency

This document and `src/nextloop/contracts.py` are the only integration dependency between nextloop and its consumers. Public API compatibility (`API_VERSION`) and event-schema compatibility (`EVENT_SCHEMA_VERSION`) are versioned independently.

## Public shape

```text
AgentLoop.run(TurnContext) -> TurnOutcome
AgentLoop.steer(session_id, content, *, input_id=None) -> bool
AgentLoop.cancel(session_id, cause="user_stop") -> bool
AgentLoop.recover(session_id, store, delivery) -> RecoveryOutcome
```

`steer` and `cancel` return `False` when there is no active Turn or final commitment has already happened. Input after final commitment belongs to a later Turn.

### Coordinate system

Every execution fact is indexed by 4D integer coordinates plus a writer fence:

- `session_id: str`
- `seq: int` (1-based per Session)
- `turn: int` (1-based per Session)
- `step: int` (1-based per Turn; one logical model call + Tool batch)
- `attempt: int` (1-based per Step; Provider retry counter)
- `writer_epoch: int` (monotonic fencing token per Session writer attachment)

## The Only 4 Adapter Seams

Context assembly and Compaction are built-in mechanisms configured through `LoopConfig`.

### 1. EventStore

- `attach_writer(session_id) -> (writer_epoch, last_seq, last_turn)`
- `append(session_id, writer_epoch, expected_seq, events) -> Sequence[Event]`
- `load_surface(session_id) -> Sequence[Event]` — O(Δ) query from the latest `SurfaceOp.EPOCH` watermark where `surface_op != NONE`
- `load_recovery(session_id) -> Sequence[Event]` — full ordered history for deterministic crash repair

### 2. ModelProvider

- `complete(request, *, attempt, cancel) -> ModelResponse`
- Configured externally with `openai` or `anthropic` protocol, `model_id`, `base_url`, credentials, and optional defaults.
- Never persists connection configuration into Trajectory, executes Tools, or runs a hidden retry loop.

### 3. ToolRuntime

- `catalog() -> Sequence[ToolSpec]`
- `validate(calls) -> (validated_calls, issues)` — validates the whole proposed batch before any dispatch
- `execute(call, arguments, *, session_id, turn, step, cancel) -> ToolOutcome`

Each `ToolSpec` declares `mode = PARALLEL | EXCLUSIVE`, optional `resources`, and optional `timeout_seconds`.

The five closed `ToolOutcome` variants are:

- `Completed(content, is_error=False, concludes_turn=False, structured=None, assets=(), additional_messages=(), release_resources=())`
  - `concludes_turn=True` delivers `content` as the Turn's final reply and ends the Turn without a `WaitingRequest` suspension state.
  - `additional_messages` appends WorkingContext or other user-level messages to later requests.
  - `release_resources` records verified resource release.
- `Skipped(reason)`
- `Cancelled(settled, partial_content="", resource_locks=())`
- `UncertainSettled(content, assets=())`
- `UncertainHanging(content, resource_locks=())`

`ToolRegistry` is the built-in implementation. `BatchExecutor` owns bounded parallel pools, exclusive barriers, failure truncation, and model-order result collection.

### 4. DeliveryOwner

- `deliver(message: FinalMessage) -> None`
- Delivers a committed `final_message_id` after `final/message + turn/end` are durable.
- Recovery retries only external delivery and never reruns the model or Tools.

## Directory-based Skills

`SkillRegistry(paths)` recursively discovers `SKILL.md`, validates frontmatter, and exposes `list()` for metadata and `load(name)` for the current instruction body. `skill_view(registry)` adapts a registry to an ordinary read-only Tool handler. Skill loading never executes code.

See [Directory-based SkillRegistry](skills.md).

## Canonical documents

- [DSH-aligned AgentLoop](agent-loop.md)
- [ReAct execution protocol](react-protocol.md)
- [ContextBuilder](context-builder.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)
- [ADR-0003](../adr/0003-python-and-centralized-contracts.md)
