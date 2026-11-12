from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

from .contracts import (
    EVENT_SCHEMA_VERSION,
    Cancelled,
    Completed,
    DeliveryOwner,
    Event,
    EventStore,
    EventType,
    InvariantViolation,
    Message,
    NewEvent,
    PendingInput,
    RecoveryOutcome,
    Skipped,
    SteerResult,
    SteerStatus,
    SurfaceOp,
    ToolCall,
    ToolEffect,
    TurnContext,
    TurnOutcome,
    TurnStatus,
    UncertainHanging,
    new_id,
)
from .driver import SessionActor, SessionExecution, TurnDriver
from .events.approval import (
    approval_decision_event,
    approval_expired,
    pending_approval,
)
from .events.input import user_input_events
from .events.outcome import BatchAction, reconciliation_event, settle_outcomes
from .events.pending import completed_input_outcome, pending_inputs
from .events.project import message_to_data
from .events.repair import active_resource_locks, pending_final_messages, repair_events
from .execute.batch import BatchExecutor


def _has_successful_reconciliation(events: Sequence[Event], operation_id: str) -> bool:
    return any(
        event.event_type == EventType.RECONCILIATION_RESULT
        and event.payload.get("operation_id") == operation_id
        and event.payload.get("outcome", {}).get("kind") == "completed"
        and not event.payload.get("outcome", {}).get("is_error", False)
        for event in events
    )


def _is_uncertain_result(event: Event) -> bool:
    outcome = event.payload.get("outcome", {})
    kind = outcome.get("kind")
    return kind in ("uncertain_hanging", "uncertain_settled") or (
        kind == "cancelled" and not outcome.get("settled", False)
    )


class AgentLoop:
    def __init__(self) -> None:
        self._actors: dict[str, SessionActor] = {}
        self._active: dict[str, SessionExecution] = {}
        self._resource_locks: dict[str, set[str]] = {}
        self._stores: dict[str, EventStore] = {}

    async def _prepare_execution(
        self,
        context: TurnContext,
        actor: SessionActor,
        writer_epoch: int,
        last_seq: int,
        last_turn: int,
        history: Sequence[Event],
        resource_locks: set[str],
    ) -> SessionExecution:
        execution = SessionExecution(
            context=context,
            actor=actor,
            writer_epoch=writer_epoch,
            seq=last_seq,
            turn=last_turn + 1,
            resource_locks=resource_locks,
        )
        execution.surface = tuple(await context.store.load_surface(context.session_id))
        execution.accepted_inputs = {
            str(event.payload["input_id"]): str(event.payload.get("content", ""))
            for event in history
            if event.event_type == EventType.INPUT_ACCEPTED and event.payload.get("input_id")
        }
        if (
            context.input_id in execution.accepted_inputs
            and execution.accepted_inputs[context.input_id] != context.initial_input
        ):
            raise InvariantViolation("input_id reused with different content")
        execution.recovered_inputs.extend(
            (item.input_id, item.content) for item in pending_inputs(history)
        )
        return execution

    def _activate_execution(self, execution: SessionExecution) -> None:
        context = execution.context
        self._active[context.session_id] = execution
        deadline = context.config.turn_deadline_seconds
        if deadline is not None:
            execution.deadline_task = asyncio.create_task(self._expire(execution, deadline))
        interval = getattr(context.store, "lease_seconds", None)
        if interval is not None:
            execution.heartbeat_task = asyncio.create_task(self._heartbeat(execution, interval / 3))

    async def _cleanup_execution(self, execution: SessionExecution) -> None:
        tasks = [execution.deadline_task, execution.heartbeat_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        if execution.preview is not None:
            await execution.preview.close()
        self._active.pop(execution.session_id, None)

    async def run(self, context: TurnContext) -> TurnOutcome:
        if not context.input_id.strip():
            raise ValueError("input_id must be non-empty")
        self._stores[context.session_id] = context.store
        actor = self._actors.setdefault(context.session_id, SessionActor())
        async with actor.run_lock:
            writer_epoch, last_seq, last_turn = await context.store.attach_writer(
                context.session_id
            )
            history = await context.store.load_recovery(context.session_id)
            replayed = completed_input_outcome(
                history, context.session_id, context.input_id, context.initial_input
            )
            if replayed is not None:
                return replayed
            if pending_approval(history) is not None:
                raise InvariantViolation("Approval pending; use resume")
            execution = await self._prepare_execution(
                context,
                actor,
                writer_epoch,
                last_seq,
                last_turn,
                history,
                set(self._resource_locks.get(context.session_id, set())),
            )
            self._activate_execution(execution)
            try:
                await execution.append(
                    [
                        NewEvent(
                            event_type=EventType.INPUT_ACCEPTED,
                            turn=execution.turn,
                            step=0,
                            payload={
                                "input_id": context.input_id,
                                "content": context.initial_input,
                            },
                        ),
                        NewEvent(event_type=EventType.TURN_START, turn=execution.turn, step=0),
                    ]
                )
                outcome = await TurnDriver(execution).run()
                self._resource_locks[context.session_id] = set(execution.resource_locks)
                return outcome
            finally:
                await self._cleanup_execution(execution)

    async def _expire(self, execution: SessionExecution, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self._request_cancel(execution, "turn_deadline_exceeded")

    async def _lease_writer_available(self, store: EventStore, session_id: str) -> bool:
        if getattr(store, "lease_seconds", None) is None:
            return True
        return await store.has_writer(session_id)

    async def _heartbeat(self, execution: SessionExecution, seconds: float) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await execution.context.store.renew_writer(
                    execution.session_id, execution.writer_epoch
                )
            except Exception:
                execution.cancel_cause = "writer_lease_lost"
                execution.cancel.set()
                return

    async def steer(self, session_id: str, content: str, *, input_id: str | None = None) -> bool:
        result = await self.steer_result(session_id, content, input_id=input_id)
        if result.status == SteerStatus.INVALID_INPUT and input_id is not None:
            raise ValueError("input_id must be non-empty")
        return result.status == SteerStatus.ACCEPTED

    async def steer_result(
        self, session_id: str, content: str, *, input_id: str | None = None
    ) -> SteerResult:
        if (input_id is not None and not input_id.strip()) or not content.strip():
            return SteerResult(SteerStatus.INVALID_INPUT, input_id)
        execution = self._active.get(session_id)
        if execution is not None:
            async with execution.actor.commit_lock:
                if execution.final_committed:
                    return SteerResult(SteerStatus.TURN_CLOSED, input_id)
                try:
                    await execution.context.store.check_writer(session_id, execution.writer_epoch)
                except InvariantViolation:
                    return SteerResult(SteerStatus.NOT_WRITER, input_id)
                if input_id in execution.accepted_inputs:
                    if execution.accepted_inputs[input_id] != content:
                        raise InvariantViolation("input_id reused with different content")
                    return SteerResult(SteerStatus.ACCEPTED, input_id)
                if len(execution.actor.inbox) >= execution.context.config.max_pending_inputs:
                    return SteerResult(SteerStatus.QUEUE_FULL, input_id)
                accepted_id = input_id or new_id("in")
                await execution._append_locked(
                    [
                        NewEvent(
                            event_type=EventType.INPUT_ACCEPTED,
                            turn=execution.turn,
                            step=execution.step,
                            payload={"input_id": accepted_id, "content": content},
                        )
                    ]
                )
                execution.actor.inbox.append((accepted_id, content))
                return SteerResult(SteerStatus.ACCEPTED, accepted_id)

        store = self._stores.get(session_id)
        if store is None:
            return SteerResult(SteerStatus.SESSION_MISSING, input_id)
        history = await store.load_recovery(session_id)
        if not history:
            return SteerResult(SteerStatus.SESSION_MISSING, input_id)
        if pending_approval(history) is not None:
            return SteerResult(SteerStatus.APPROVAL_PENDING, input_id)
        if not await self._lease_writer_available(store, session_id):
            return SteerResult(SteerStatus.NOT_WRITER, input_id)
        return SteerResult(SteerStatus.TURN_CLOSED, input_id)

    async def cancel(self, session_id: str, cause: str = "user_stop") -> bool:
        execution = self._active.get(session_id)
        if execution is not None:
            return await self._request_cancel(execution, cause)
        store = self._stores.get(session_id)
        if store is None:
            return False
        if not await self._lease_writer_available(store, session_id):
            return False
        request = pending_approval(await store.load_recovery(session_id))
        if request is None:
            return False
        actor = self._actors.setdefault(session_id, SessionActor())
        async with actor.run_lock:
            request = pending_approval(await store.load_recovery(session_id))
            if request is None:
                return False
            epoch, last_seq, last_turn = await store.attach_writer(session_id)
            await store.append(
                session_id,
                epoch,
                last_seq + 1,
                [
                    NewEvent(
                        EventType.CANCELLATION_REQUESTED,
                        turn=last_turn,
                        step=0,
                        payload={"cause": cause},
                    ),
                    approval_decision_event(
                        EventType.APPROVAL_CANCELLED,
                        request,
                        {},
                        turn=last_turn,
                        step=0,
                        cause=cause,
                    ),
                ],
            )
            return True

    async def _request_cancel(self, execution: SessionExecution, cause: str) -> bool:
        async with execution.actor.commit_lock:
            if execution.cancel_cause is not None:
                return False
            if execution.final_committed and cause != "turn_deadline_exceeded":
                return False
            if getattr(execution.context.store, "lease_seconds", None) is not None:
                try:
                    await execution.context.store.check_writer(
                        execution.session_id, execution.writer_epoch
                    )
                except InvariantViolation:
                    return False
            await execution._append_locked(
                [
                    NewEvent(
                        event_type=EventType.CANCELLATION_REQUESTED,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"cause": cause},
                    )
                ]
            )
            execution.cancel_cause = cause
            execution.cancel.set()
            return True

    async def pending_inputs(self, session_id: str, store: EventStore) -> tuple[PendingInput, ...]:
        return pending_inputs(await store.load_recovery(session_id))

    async def resource_locks(self, session_id: str, store: EventStore) -> frozenset[str]:
        return active_resource_locks(await store.load_recovery(session_id))

    async def release_resource(
        self,
        session_id: str,
        store: EventStore,
        resource: str,
        *,
        actor: str,
        evidence: str,
    ) -> bool:
        if not resource.strip() or not actor.strip() or not evidence.strip():
            raise ValueError("resource, actor, and evidence must be non-empty")
        if resource not in await self.resource_locks(session_id, store):
            return False
        epoch, last_seq, last_turn = await store.attach_writer(session_id)
        await store.append(
            session_id,
            epoch,
            last_seq + 1,
            [
                NewEvent(
                    EventType.RESOURCE_RELEASED,
                    turn=last_turn,
                    step=0,
                    payload={"resource": resource, "actor": actor, "evidence": evidence},
                )
            ],
        )
        self._resource_locks.setdefault(session_id, set()).discard(resource)
        return True

    async def resume(
        self,
        context: TurnContext,
        *,
        decisions: Mapping[str, bool] | None = None,
        reject_all: bool = False,
        new_input: str | None = None,
    ) -> TurnOutcome:
        if new_input is not None:
            context = replace(context, initial_input=new_input)
        history = await context.store.load_recovery(context.session_id)
        request = pending_approval(history)
        if request is None:
            raise InvariantViolation("No pending approval")
        expected = {item.operation_id for item in request.items}
        approval_id = request.approval_id
        if reject_all:
            resolved = {operation_id: False for operation_id in expected}
        else:
            if decisions is None or set(decisions) != expected:
                raise InvariantViolation("Approval decisions must cover every original operation")
            resolved = dict(decisions)
        if any(type(value) is not bool for value in resolved.values()):
            raise InvariantViolation("Approval decisions must be boolean")

        actor = self._actors.setdefault(context.session_id, SessionActor())
        async with actor.run_lock:
            history = await context.store.load_recovery(context.session_id)
            request = pending_approval(history)
            if request is None:
                raise InvariantViolation("No pending approval")
            if (
                request.approval_id != approval_id
                or {item.operation_id for item in request.items} != expected
            ):
                raise InvariantViolation("Approval changed; reload decisions")
            if any(
                event.event_type == EventType.TOOL_CALL
                and event.payload.get("operation_id") in expected
                for event in history
            ):
                raise InvariantViolation("Approval dispatch already started; recover first")
            decision_cause = "approval_expired" if approval_expired(request) else None
            if decision_cause is not None:
                resolved = {operation_id: False for operation_id in expected}
            if all(resolved.values()):
                specs = {spec.name: spec for spec in context.tools.catalog()}
                for item in request.items:
                    spec = specs.get(item.name)
                    approval = spec.approval if spec is not None else None
                    policy_matches = (
                        approval is not None and approval.version == item.policy_version
                        if item.policy_version
                        else approval is None
                    )
                    if (
                        spec is None
                        or item.schema_version != EVENT_SCHEMA_VERSION
                        or not policy_matches
                    ):
                        raise InvariantViolation(
                            "Approval policy or Event schema changed; reload decisions"
                        )
            writer_epoch, last_seq, last_turn = await context.store.attach_writer(
                context.session_id
            )
            execution = await self._prepare_execution(
                context,
                actor,
                writer_epoch,
                last_seq,
                last_turn,
                history,
                set(active_resource_locks(history)),
            )
            self._activate_execution(execution)
            try:
                await execution.append(
                    [
                        NewEvent(
                            EventType.INPUT_ACCEPTED,
                            turn=execution.turn,
                            step=0,
                            payload={
                                "input_id": context.input_id,
                                "content": context.initial_input,
                            },
                        ),
                        NewEvent(EventType.TURN_START, turn=execution.turn, step=0),
                    ]
                )
                approved = all(resolved.values())
                execution.step = 1
                execution.open_step = True
                await execution.append(
                    [
                        approval_decision_event(
                            EventType.APPROVAL_GRANTED if approved else EventType.APPROVAL_REJECTED,
                            request,
                            resolved,
                            turn=execution.turn,
                            step=1,
                            cause=decision_cause,
                        ),
                        NewEvent(EventType.STEP_START, turn=execution.turn, step=1),
                    ]
                )
                calls = [
                    ToolCall(
                        item.call_id,
                        item.name,
                        item.canonical_arguments,
                        operation_id=item.operation_id,
                    )
                    for item in request.items
                ]
                if approved:

                    async def commit(events: Sequence[NewEvent]) -> None:
                        await execution.append(
                            [
                                replace(
                                    event,
                                    payload={
                                        **event.payload,
                                        "origin_turn": request.origin_turn,
                                        "origin_step": request.origin_step,
                                        "origin_kind": "approval",
                                    },
                                )
                                for event in events
                            ]
                        )

                    batch = await BatchExecutor(
                        context.tools,
                        context.config.max_parallel_tools,
                        admit=execution.admit_tool_batch,
                    ).execute(
                        calls,
                        session_id=context.session_id,
                        turn=execution.turn,
                        step=1,
                        cancel=execution.cancel,
                        active_resources=frozenset(execution.resource_locks),
                        commit=commit,
                        check_writer=lambda: context.store.check_writer(
                            context.session_id, writer_epoch
                        ),
                        approval_checked=True,
                    )
                    if batch.issues or batch.budget_reason:
                        raise InvariantViolation("Approved batch could not be admitted")
                    outcomes = batch.outcomes
                    blocked = batch.blocked_resources
                    timings = batch.timings
                else:
                    validated, issues = context.tools.validate(calls)
                    if issues:
                        raise InvariantViolation("; ".join(issue.message for issue in issues))
                    outcomes = tuple(
                        (
                            item.call,
                            item.spec,
                            Skipped(decision_cause or "Rejected by approval"),
                        )
                        for item in validated
                    )
                    blocked = frozenset()
                    timings = {}
                settled = settle_outcomes(
                    outcomes,
                    turn=execution.turn,
                    step=1,
                    pacing_step_interval=context.config.pacing_step_interval,
                    blocked_resources=blocked,
                    timings=timings,
                    result_bytes=context.config.tool_result_bytes,
                    full_max_bytes=context.config.tool_result_full_max_bytes,
                )
                events = [
                    replace(
                        event,
                        payload={
                            **event.payload,
                            "origin_turn": request.origin_turn,
                            "origin_step": request.origin_step,
                            "origin_kind": "approval",
                        },
                    )
                    if event.event_type == EventType.TOOL_RESULT
                    else event
                    for event in settled.events
                ]
                execution.resource_locks.update(settled.acquired_resources)
                execution.resource_locks.difference_update(settled.released_resources)
                terminal = {
                    BatchAction.BLOCKED: TurnStatus.BLOCKED,
                    BatchAction.CANCELLED: TurnStatus.CANCELLED,
                    BatchAction.FAILED: TurnStatus.FAILED,
                }
                if settled.action in terminal:
                    self._resource_locks[context.session_id] = set(execution.resource_locks)
                    return await TurnDriver(execution)._finalize(
                        terminal[settled.action], settled.error, facts=events
                    )
                if settled.action == BatchAction.FINALIZE:
                    await execution.append(events)
                    outcome = await TurnDriver(execution)._finish_success(settled.final_content)
                    if outcome is not None:
                        return outcome
                    events = []

                inputs = [
                    *execution.recovered_inputs,
                    *execution.actor.inbox,
                    (context.input_id, context.initial_input),
                ]
                execution.recovered_inputs.clear()
                execution.actor.inbox.clear()
                user_events = user_input_events(inputs, turn=execution.turn, step=1)
                await execution.append(
                    [
                        *events,
                        *user_events,
                        NewEvent(
                            EventType.STEP_END,
                            turn=execution.turn,
                            step=1,
                            payload={"status": "approval_resumed"},
                        ),
                    ]
                )
                execution.open_step = False
                self._resource_locks[context.session_id] = set(execution.resource_locks)
                return await TurnDriver(execution).run()
            finally:
                await self._cleanup_execution(execution)

    async def retry_operation(self, context: TurnContext, operation_id: str) -> TurnOutcome:
        history = await context.store.load_recovery(context.session_id)
        original_result = next(
            (
                event
                for event in history
                if event.event_type == EventType.TOOL_RESULT
                and event.payload.get("operation_id") == operation_id
                and _is_uncertain_result(event)
            ),
            None,
        )
        if original_result is None:
            raise InvariantViolation("Operation must have a recovered uncertain result")
        original = next(
            (
                event
                for event in history
                if event.event_type == EventType.TOOL_CALL
                and event.turn == original_result.turn
                and event.step == original_result.step
                and event.payload.get("operation_id") == operation_id
            ),
            None,
        )
        if original is None:
            raise InvariantViolation(f"Unknown operation_id: {operation_id}")
        if _has_successful_reconciliation(history, operation_id):
            raise InvariantViolation("Operation is already reconciled")

        call = ToolCall(
            call_id=str(original.payload["call_id"]),
            name=str(original.payload["name"]),
            arguments=str(original.payload["arguments"]),
            operation_id=operation_id,
        )
        validated, issues = context.tools.validate([call])
        if issues:
            raise InvariantViolation("; ".join(issue.message for issue in issues))
        spec = validated[0].spec
        if spec.effect == ToolEffect.NON_IDEMPOTENT:
            raise InvariantViolation("NON_IDEMPOTENT operations cannot be retried")

        actor = self._actors.setdefault(context.session_id, SessionActor())
        async with actor.run_lock:
            history = await context.store.load_recovery(context.session_id)
            if _has_successful_reconciliation(history, operation_id):
                raise InvariantViolation("Operation is already reconciled")
            writer_epoch, last_seq, last_turn = await context.store.attach_writer(
                context.session_id
            )
            execution = await self._prepare_execution(
                context,
                actor,
                writer_epoch,
                last_seq,
                last_turn,
                history,
                set(active_resource_locks(history)),
            )
            self._activate_execution(execution)
            try:
                await execution.append(
                    [
                        NewEvent(
                            EventType.INPUT_ACCEPTED,
                            turn=execution.turn,
                            step=0,
                            payload={
                                "input_id": context.input_id,
                                "content": context.initial_input,
                            },
                        ),
                        NewEvent(EventType.TURN_START, turn=execution.turn, step=0),
                    ]
                )
                execution.step = 1
                await execution.append(
                    [
                        NewEvent(EventType.STEP_START, turn=execution.turn, step=1),
                        NewEvent(
                            EventType.USER_MESSAGE,
                            turn=execution.turn,
                            step=1,
                            surface_op=SurfaceOp.APPEND,
                            payload={
                                "input_id": context.input_id,
                                "message": message_to_data(
                                    Message(role="user", content=context.initial_input)
                                ),
                            },
                        ),
                    ]
                )

                async def commit(events: Sequence[NewEvent]) -> None:
                    await execution.append(
                        [
                            replace(
                                event,
                                payload={
                                    **event.payload,
                                    "origin_turn": original.turn,
                                    "origin_step": original.step,
                                },
                            )
                            for event in events
                        ]
                    )

                batch = await BatchExecutor(
                    context.tools, context.config.max_parallel_tools
                ).execute(
                    [call],
                    session_id=context.session_id,
                    turn=execution.turn,
                    step=1,
                    cancel=execution.cancel,
                    active_resources=frozenset(),
                    commit=commit,
                    check_writer=lambda: context.store.check_writer(
                        context.session_id, writer_epoch
                    ),
                )
                if batch.issues or batch.budget_reason or len(batch.outcomes) != 1:
                    raise InvariantViolation("Reconciliation could not be admitted")
                _, spec, outcome = batch.outcomes[0]
                reconciliation_locks = {
                    lock
                    for event in history
                    if event.event_type == EventType.RECONCILIATION_RESULT
                    and event.payload.get("operation_id") == operation_id
                    for lock in event.payload.get("outcome", {}).get("resource_locks", ())
                }
                released = (
                    tuple(
                        sorted(
                            {
                                *original.payload.get("resources", ()),
                                *original_result.payload.get("outcome", {}).get(
                                    "resource_locks", ()
                                ),
                                *reconciliation_locks,
                            }
                        )
                    )
                    if isinstance(outcome, Completed) and not outcome.is_error
                    else ()
                )
                reconciliation = reconciliation_event(
                    call,
                    outcome,
                    origin_turn=original.turn,
                    origin_step=original.step,
                    turn=execution.turn,
                    step=1,
                    release_resources=released,
                    timing=batch.timings.get(call.call_id),
                    spec=spec,
                    result_bytes=context.config.tool_result_bytes,
                )
                if isinstance(outcome, Completed):
                    status = TurnStatus.FAILED if outcome.is_error else TurnStatus.SUCCEEDED
                    error = outcome.content if outcome.is_error else None
                elif isinstance(outcome, Cancelled):
                    status = TurnStatus.CANCELLED
                    error = outcome.partial_content
                elif isinstance(outcome, UncertainHanging):
                    status = TurnStatus.BLOCKED
                    error = outcome.content
                else:
                    status = TurnStatus.FAILED
                    error = "Reconciliation remains uncertain"
                result = await TurnDriver(execution)._finalize(
                    status, error, facts=[reconciliation]
                )
                self._resource_locks[context.session_id] = set(
                    active_resource_locks(await context.store.load_recovery(context.session_id))
                )
                return result
            finally:
                await self._cleanup_execution(execution)

    async def recover(
        self,
        session_id: str,
        store: EventStore,
        delivery: DeliveryOwner,
        *,
        force: bool = False,
    ) -> RecoveryOutcome:
        self._stores[session_id] = store
        actor = self._actors.setdefault(session_id, SessionActor())
        async with actor.run_lock:
            if session_id in self._active:
                raise RuntimeError("Cannot recover a Session with an active Turn")
            writer_epoch, last_seq, _ = await store.attach_writer(session_id, force=force)
            events = list(await store.load_recovery(session_id))
            repaired = repair_events(events)
            seq = last_seq

            async def append(new_events: Sequence[NewEvent]) -> None:
                nonlocal seq
                if not new_events:
                    return
                committed = await store.append(session_id, writer_epoch, seq + 1, new_events)
                seq = committed[-1].seq

            await append(repaired.events)
            self._resource_locks[session_id] = set(repaired.active_resource_locks)
            delivered = []
            delivery_unknown = []
            for message in pending_final_messages(events):
                try:
                    await delivery.deliver(message)
                    await append(
                        [
                            NewEvent(
                                event_type=EventType.DELIVERY_COMMITTED,
                                turn=message.turn,
                                step=message.step,
                                payload={
                                    "final_message_id": message.final_message_id,
                                    "recovered": True,
                                },
                            )
                        ]
                    )
                    delivered.append(message.final_message_id)
                except Exception as exc:
                    await append(
                        [
                            NewEvent(
                                event_type=EventType.DELIVERY_UNKNOWN,
                                turn=message.turn,
                                step=message.step,
                                payload={
                                    "final_message_id": message.final_message_id,
                                    "error": str(exc),
                                    "recovered": True,
                                },
                            )
                        ]
                    )
                    delivery_unknown.append(message.final_message_id)
            return RecoveryOutcome(
                session_id=session_id,
                writer_epoch=writer_epoch,
                interrupted_turns=repaired.interrupted_turns,
                delivered_final_ids=tuple(delivered),
                delivery_unknown_final_ids=tuple(delivery_unknown),
                active_resource_locks=repaired.active_resource_locks,
            )
