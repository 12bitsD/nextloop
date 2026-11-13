# Loop interface and ownership

Status: **v1.1 implemented for 1.1.0rc1**. Public contracts live in `src/nextloop/contracts.py`.

## Integration and versions

Consumers integrate through this document and `contracts.py`. Public API compatibility (`API_VERSION`) and Event Schema compatibility (`EVENT_SCHEMA_VERSION`) are versioned independently. See [Architecture and import contract](architecture.md) for stable module paths and root re-exports.

## AgentLoop operations

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

`steer` and `cancel` return `False` if no Turn is active or the final response is already committed. New input after that point belongs to a later Turn.

Pending Input is derived from the Event log by `input_id`. A new Turn records recovered Pending as `USER_MESSAGE` at its first Step boundary without duplicating input already in the active inbox.

### Coordinates

Each execution fact has these identifiers:

- `session_id: str` — the Session.
- `seq: int` — Event order within the Session, starting at 1.
- `turn: int` — Turn number within the Session, starting at 1.
- `step: int` — one logical Model call and Tool batch within a Turn, starting at 1.
- `attempt: int` — Provider attempt within a Step, starting at 1.
- `writer_epoch: int` — fencing token for the current writer attachment.

## Adapter interfaces

`LoopConfig` controls context assembly and Compaction. Other extension points include `PreviewSink`, Exporters, Skills, and stdio/MCP workers.

### 1. EventStore

- `session_ids() -> Sequence[str]` — list non-tombstoned Sessions with committed Events in stable order for Outbox export.
- `delete_session(session_id) -> None` — fence the Session and remove local Event state. Repeated deletion is safe; later reads and writes raise `SessionDeletedError`.
- `attach_writer(session_id) -> (writer_epoch, last_seq, last_turn)`
- `check_writer(session_id, writer_epoch) -> None` — reject a stale writer before Tool dispatch.
- `append(session_id, writer_epoch, expected_seq, events) -> Sequence[Event]` — check epoch and sequence, then insert atomically. Identical retries are idempotent.
- `load_surface(session_id) -> Sequence[Event]` — load model-visible Events from the latest `SurfaceOp.EPOCH`, plus `INPUT_ACCEPTED` and protocol corrections.
- `load_recovery(session_id) -> Sequence[Event]` — load full ordered history for crash repair and Pending Projection.

Third-party Stores must pass `nextloop.testing.check_event_store()`.

Writer epochs are not time-based leases. Optional `owner_id` and `lease_seconds` add owner/expiry checks, heartbeat renewal, and same-host takeover. They do not provide distributed coordination. Decorators must delegate `session_ids()` and `delete_session()` so export and deletion cannot bypass the wrapped Store.

**Migration from 0.5.0rc2 and earlier:** add `check_writer(session_id, writer_epoch)`. It must read the committed epoch and raise `InvariantViolation("Stale writer_epoch")` when the supplied epoch is stale. Decorators must delegate the check. A no-op default is not allowed because it would reopen the stale-dispatch window after `TOOL_CALL`.

**Migration from 0.8:** add idempotent `delete_session(session_id)` with fail-closed tombstones. Decorators must delegate deletion and clear derived Session state. Built-in SQLite Stores create tombstone schema only when deletion starts; opening old fixtures for read-only upcasting does not rewrite them. Whole-Session deletion must also call `SQLiteOutbox.delete_session()`, without assuming a cross-database transaction.

Initial input and steer IDs must be non-empty. After success, the same initial ID and content return the original `TurnOutcome` without new execution; unfinished or unsuccessful work may start a new Turn. Identical steer retries do not duplicate accepted input. Reusing any ID with different content raises `InvariantViolation` before committing another `INPUT_ACCEPTED`.

### 2. ModelProvider

- `complete(request, *, attempt, cancel) -> ModelResponse`
- Configure `openai` or `anthropic`, the model, URL, credentials, and defaults outside the Loop.
- Never store connection configuration in Trajectory, execute Tools, or hide a retry loop.

### 3. ToolRuntime

- `catalog() -> Sequence[ToolSpec]`
- `validate(calls) -> (validated_calls, issues)` — validate the whole proposed batch before dispatch.
- `execute(call, arguments, *, session_id, turn, step, cancel) -> ToolOutcome`

Each `ToolSpec` declares `mode = PARALLEL | EXCLUSIVE`, optional `resources`, and optional `timeout_seconds`.

`ToolOutcome` has five closed variants:

- `Completed(content, is_error=False, concludes_turn=False, structured=None, assets=(), additional_messages=(), release_resources=())`
  - `concludes_turn=True` delivers `content` as the final response and ends the Turn.
  - `additional_messages` adds WorkingContext or other user-level messages to later requests.
  - `release_resources` records verified resource release.
- `Skipped(reason)`
- `Cancelled(settled, partial_content="", resource_locks=())`
- `UncertainSettled(content, assets=())`
- `UncertainHanging(content, resource_locks=())`

`ToolRegistry` is the built-in runtime. `BatchExecutor` handles bounded parallel execution, exclusive barriers, failure truncation, and model-order result collection.

### 4. DeliveryOwner

- `deliver(message: FinalMessage) -> None`
- Deliver a committed `final_message_id` only after `final/message` and `turn/end` are durable.
- Recovery retries Delivery only. It never reruns the model or Tools.

## Directory-based Skills

`SkillRegistry(paths)` finds `SKILL.md` files, validates their frontmatter, and exposes `list()` for metadata and `load(name)` for instructions. `skill_view(registry)` exposes a registry as a read-only Tool. Loading a Skill never executes code.

See [Directory-based SkillRegistry](skills.md).

## Related documents

- [DSH-aligned AgentLoop](agent-loop.md)
- [ReAct execution protocol](react-protocol.md)
- [ContextBuilder](context-builder.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)
- [ADR-0003](../adr/0003-python-and-centralized-contracts.md)
