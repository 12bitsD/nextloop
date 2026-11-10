from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .contracts import (
    DeliveryOwner,
    EventStore,
    EventType,
    NewEvent,
    PendingInput,
    RecoveryOutcome,
    TurnContext,
    TurnOutcome,
)
from .driver import SessionActor, SessionExecution, TurnDriver
from .events.pending import pending_inputs
from .events.repair import pending_final_messages, repair_events


class AgentLoop:
    def __init__(self) -> None:
        self._actors: dict[str, SessionActor] = {}
        self._active: dict[str, SessionExecution] = {}
        self._resource_locks: dict[str, set[str]] = {}

    async def run(self, context: TurnContext) -> TurnOutcome:
        actor = self._actors.setdefault(context.session_id, SessionActor())
        async with actor.run_lock:
            writer_epoch, last_seq, last_turn = await context.store.attach_writer(
                context.session_id
            )
            execution = SessionExecution(
                context=context,
                actor=actor,
                writer_epoch=writer_epoch,
                seq=last_seq,
                turn=last_turn + 1,
                resource_locks=set(self._resource_locks.get(context.session_id, set())),
            )
            execution.surface = tuple(await context.store.load_surface(context.session_id))
            self._active[context.session_id] = execution
            deadline = context.config.turn_deadline_seconds
            if deadline is not None:
                execution.deadline_task = asyncio.create_task(self._expire(execution, deadline))
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
                if execution.deadline_task is not None:
                    execution.deadline_task.cancel()
                    await asyncio.gather(execution.deadline_task, return_exceptions=True)
                if execution.preview is not None:
                    await execution.preview.close()
                self._active.pop(context.session_id, None)

    async def _expire(self, execution: SessionExecution, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self._request_cancel(execution, "turn_deadline_exceeded")

    async def steer(self, session_id: str, content: str, *, input_id: str | None = None) -> bool:
        execution = self._active.get(session_id)
        if execution is None or not content.strip():
            return False
        async with execution.actor.commit_lock:
            if execution.final_committed:
                return False
            accepted_id = (
                input_id or f"in_{len(execution.actor.inbox) + 1}_{execution.turn}_{execution.step}"
            )
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
            return True

    async def cancel(self, session_id: str, cause: str = "user_stop") -> bool:
        execution = self._active.get(session_id)
        if execution is None:
            return False
        return await self._request_cancel(execution, cause)

    async def _request_cancel(self, execution: SessionExecution, cause: str) -> bool:
        async with execution.actor.commit_lock:
            if execution.cancel_cause is not None:
                return False
            if execution.final_committed and cause != "turn_deadline_exceeded":
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

    async def recover(
        self,
        session_id: str,
        store: EventStore,
        delivery: DeliveryOwner,
    ) -> RecoveryOutcome:
        actor = self._actors.setdefault(session_id, SessionActor())
        async with actor.run_lock:
            if session_id in self._active:
                raise RuntimeError("Cannot recover a Session with an active Turn")
            writer_epoch, last_seq, _ = await store.attach_writer(session_id)
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
