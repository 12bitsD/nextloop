from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from .contracts import (
    DeliveryOwner,
    EventStore,
    EventType,
    InvariantViolation,
    NewEvent,
    PendingInput,
    RecoveryOutcome,
    SteerResult,
    SteerStatus,
    TurnContext,
    TurnOutcome,
    new_id,
)
from .driver import (
    ExecutionFrame,
    SessionActor,
    SessionExecution,
    TurnDriver,
    has_successful_reconciliation,
    run_approval_resume,
    run_reconciliation,
)
from .events.approval import approval_decision_event, pending_approval
from .events.codec import is_uncertain_result
from .events.pending import completed_input_outcome, pending_inputs
from .events.repair import active_resource_locks, pending_final_messages, repair_events


class AgentLoop:
    def __init__(self) -> None:
        self._actors: dict[str, SessionActor] = {}
        self._active: dict[str, SessionExecution] = {}
        self._stores: dict[str, EventStore] = {}

    def _frame(self, context: TurnContext) -> ExecutionFrame:
        actor = self._actors.setdefault(context.session_id, SessionActor())
        return ExecutionFrame(context, actor, self._active, self._stores)

    async def run(self, context: TurnContext) -> TurnOutcome:
        async with self._frame(context) as frame:
            history = frame.history
            replayed = completed_input_outcome(
                history, context.session_id, context.input_id, context.initial_input
            )
            if replayed is not None:
                return replayed
            if pending_approval(history) is not None:
                raise InvariantViolation("Approval pending; use resume")
            await frame.activate()
            await frame.begin()
            return await TurnDriver(frame).run()

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
                await execution.append_locked(
                    [
                        NewEvent(
                            EventType.INPUT_ACCEPTED,
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
            return await execution.request_cancel(cause)
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

    async def _lease_writer_available(self, store: EventStore, session_id: str) -> bool:
        if getattr(store, "lease_seconds", None) is None:
            return True
        return await store.has_writer(session_id)

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
        if session_id in self._active:
            raise RuntimeError("Cannot release resource on a Session with an active Turn")
        session_actor = self._actors.setdefault(session_id, SessionActor())
        async with session_actor.run_lock:
            if session_id in self._active:
                raise RuntimeError("Cannot release resource on a Session with an active Turn")
            history = await store.load_recovery(session_id)
            if resource not in active_resource_locks(history):
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
            return True

    async def resume(
        self,
        context: TurnContext,
        *,
        decisions: Mapping[str, bool] | None = None,
        reject_all: bool = False,
        new_input: str | None = None,
    ) -> TurnOutcome:
        if not context.input_id.strip():
            raise ValueError("input_id must be non-empty")
        self._stores[context.session_id] = context.store
        if new_input is not None:
            context = replace(context, initial_input=new_input)
        if pending_approval(await context.store.load_recovery(context.session_id)) is None:
            raise InvariantViolation("No pending approval")
        async with self._frame(context) as frame:
            return await run_approval_resume(frame, decisions=decisions, reject_all=reject_all)

    async def retry_operation(self, context: TurnContext, operation_id: str) -> TurnOutcome:
        if not context.input_id.strip():
            raise ValueError("input_id must be non-empty")
        self._stores[context.session_id] = context.store
        history = await context.store.load_recovery(context.session_id)
        if not any(
            event.event_type == EventType.TOOL_RESULT
            and event.payload.get("operation_id") == operation_id
            and is_uncertain_result(event)
            for event in history
        ):
            raise InvariantViolation("Operation must have a recovered uncertain result")
        if has_successful_reconciliation(history, operation_id):
            raise InvariantViolation("Operation is already reconciled")
        async with self._frame(context) as frame:
            return await run_reconciliation(frame, operation_id)

    async def recover(
        self,
        session_id: str,
        store: EventStore,
        delivery: DeliveryOwner,
        *,
        force: bool = False,
    ) -> RecoveryOutcome:
        self._stores[session_id] = store
        if session_id in self._active:
            raise RuntimeError("Cannot recover a Session with an active Turn")
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
