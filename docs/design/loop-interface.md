# Loop interface and ownership

Status: **v1.1 implemented for 0.8.0**, with public contracts in `src/nextloop/contracts.py`.

## Independence and version dependency

This document and `src/nextloop/contracts.py` are the integration dependency between nextloop and its consumers. Public API compatibility (`API_VERSION`) and event-schema compatibility (`EVENT_SCHEMA_VERSION`) are versioned independently. See [Architecture and import contract](architecture.md) for frozen module paths and root re-exports.

## Public shape

```text
AgentLoop.run(TurnContext) -> TurnOutcome
AgentLoop.steer(session_id, content, *, input_id=None) -> bool
AgentLoop.steer_result(session_id, content, *, input_id=None) -> SteerResult
AgentLoop.cancel(session_id, cause="user_stop") -> bool
AgentLoop.recover(session_id, store, delivery, *, force=False) -> RecoveryOutcome
AgentLoop.pending_inputs(session_id, store) -> tuple[PendingInput, ...]
AgentLoop.resume(context, *, decisions=None, reject_all=False, new_input=None) -> TurnOutcome
AgentLoop.retry_operation(context, operation_id) -> TurnOutcome
AgentLoop.resource_locks(session_id, store) -> frozenset[str]
AgentLoop.release_resource(session_id, store, resource, *, actor, evidence) -> bool
```

`steer` and `cancel` return `False` when there is no active Turn or final commitment has already happened. Input after final commitment belongs to a later Turn. Pending Input is derived from the log by `input_id`; a new Turn commits recovered Pending as USER_MESSAGE at its first Step boundary without duplicating active inbox input.

### Coordinate system

Every execution fact is indexed by 4D integer coordinates plus a writer fence:

- `session_id: str`
- `seq: int` (1-based per Session)
- `turn: int` (1-based per Session)
- `step: int` (1-based per Turn; one logical model call + Tool batch)
- `attempt: int` (1-based per Step; Provider retry counter)
- `writer_epoch: int` (monotonic fencing token per Session writer attachment)

## Core Adapter Seams

Context assembly and Compaction are built-in mechanisms configured through `LoopConfig`. Additional extension points include PreviewSink, Exporters, Skills, and stdio/MCP workers.

### 1. EventStore

- `session_ids() -> Sequence[str]` — stable enumeration of Sessions with committed Events for caller-driven Outbox export.
- `attach_writer(session_id) -> (writer_epoch, last_seq, last_turn)`
- `check_writer(session_id, writer_epoch) -> None` — rejects a stale writer immediately before Tool dispatch.
- `append(session_id, writer_epoch, expected_seq, events) -> Sequence[Event]` — validates epoch/seq and inserts atomically; identical retries are idempotent.
- `load_surface(session_id) -> Sequence[Event]` — returns model-visible Events from the latest `SurfaceOp.EPOCH` watermark plus INPUT_ACCEPTED and protocol correction facts.
- `load_recovery(session_id) -> Sequence[Event]` — full ordered history for deterministic crash repair and Pending projection.

Third-party implementations must pass `nextloop.testing.check_event_store()`. Writer epochs alone are not time-based leases. Optional `owner_id` and `lease_seconds` add same-host owner/expiry checks, heartbeat renewal, and takeover; they do not provide distributed coordination. Decorators must delegate `session_ids()` so caller-driven Outbox export can enumerate committed Sessions.

**Migration from 0.5.0rc2 and earlier:** every EventStore implementation must add `check_writer(session_id, writer_epoch)`. It must read the currently committed epoch and raise `InvariantViolation("Stale writer_epoch")` when the supplied epoch is not current. Decorators must delegate this check to their wrapped Store. No default no-op is provided because it would reopen the post-TOOL_CALL stale-dispatch window.

Initial input and explicit steer IDs must be non-empty. Reusing an ID with identical content is an idempotent retry; reusing it with different content raises `InvariantViolation` before a new INPUT_ACCEPTED is committed, preserving the original Pending input.

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
