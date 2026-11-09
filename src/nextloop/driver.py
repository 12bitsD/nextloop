from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from .contracts import (
    EventType,
    FinalMessage,
    InvariantViolation,
    Message,
    NewEvent,
    SurfaceOp,
    TurnContext,
    TurnOutcome,
    TurnStatus,
    new_id,
)
from .events.outcome import BatchAction, settle_outcomes
from .events.project import (
    compaction_events,
    latest_working_context,
    make_compaction_request,
    message_to_data,
    project_events,
    valid_compaction,
)
from .events.response import ResponseAction, decide_response, reject_batch
from .execute.batch import BatchExecutor
from .execute.model import ModelCaller


@dataclass
class SessionActor:
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inbox: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SessionExecution:
    context: TurnContext
    actor: SessionActor
    writer_epoch: int
    seq: int
    turn: int
    step: int = 0
    corrections: int = 0
    open_step: bool = False
    final_committed: bool = False
    resource_locks: set[str] = field(default_factory=set)

    @property
    def session_id(self) -> str:
        return self.context.session_id

    @property
    def cancel(self) -> asyncio.Event:
        if not hasattr(self, "_cancel"):
            self._cancel = asyncio.Event()
        return self._cancel

    async def append(self, events: Sequence[NewEvent]) -> None:
        async with self.actor.commit_lock:
            await self._append_locked(events)

    async def _append_locked(self, events: Sequence[NewEvent]) -> None:
        if not events:
            return
        committed = await self.context.store.append(
            self.session_id,
            self.writer_epoch,
            self.seq + 1,
            events,
        )
        self.seq = committed[-1].seq

    async def has_steer(self) -> bool:
        async with self.actor.commit_lock:
            return bool(self.actor.inbox)


class _CancelFinalization(Exception):
    pass


class TurnDriver:
    def __init__(self, execution: SessionExecution) -> None:
        self.execution = execution
        context = execution.context
        self.model = ModelCaller(context.provider, context.config, execution.has_steer)
        self.tools = BatchExecutor(context.tools, context.config.max_parallel_tools)

    async def run(self) -> TurnOutcome:
        execution = self.execution
        try:
            while True:
                if execution.cancel.is_set():
                    return await self._finalize(TurnStatus.CANCELLED, "Cancelled")
                await self._begin_step()
                surface = await execution.context.store.load_surface(execution.session_id)
                projection = project_events(
                    surface,
                    session_id=execution.session_id,
                    turn=execution.turn,
                    step=execution.step,
                    config=execution.context.config,
                    tools=execution.context.tools.catalog(),
                )
                if projection.compaction_reason is not None:
                    compaction = await self.model.call(
                        make_compaction_request(projection, execution.context.config),
                        execution.cancel,
                        purpose="compaction",
                    )
                    await execution.append(compaction.events)
                    if compaction.rebuild_for_steer:
                        await self._end_step("steer")
                        continue
                    if compaction.cancelled or execution.cancel.is_set():
                        return await self._finalize(
                            TurnStatus.CANCELLED, "Cancelled during compaction"
                        )
                    summary = compaction.response.content if compaction.response else ""
                    if compaction.error or not valid_compaction(summary, projection):
                        error = str(
                            compaction.error
                            or "Compaction returned an empty or non-shrinking summary"
                        )
                        return await self._finalize(TurnStatus.FAILED, error)
                    await execution.append(
                        compaction_events(
                            summary,
                            surface,
                            turn=execution.turn,
                            step=execution.step,
                        )
                    )
                    surface = await execution.context.store.load_surface(execution.session_id)
                    projection = project_events(
                        surface,
                        session_id=execution.session_id,
                        turn=execution.turn,
                        step=execution.step,
                        config=execution.context.config,
                        tools=execution.context.tools.catalog(),
                    )
                    if projection.compaction_reason is not None:
                        return await self._finalize(
                            TurnStatus.FAILED,
                            "Request still exceeds capacity after one compaction",
                        )

                result = await self.model.call(projection.request, execution.cancel)
                await execution.append(result.events)
                if result.rebuild_for_steer:
                    await self._end_step("steer")
                    continue
                if result.cancelled or execution.cancel.is_set():
                    return await self._finalize(TurnStatus.CANCELLED, "Cancelled")
                if result.error or result.response is None:
                    return await self._finalize(
                        TurnStatus.FAILED,
                        str(result.error or "Provider returned no response"),
                    )
                attempt = result.events[-1].attempt if result.events else 1
                decision = decide_response(
                    result.response,
                    turn=execution.turn,
                    step=execution.step,
                    attempt=attempt,
                    corrections=execution.corrections,
                    max_corrections=execution.context.config.max_protocol_corrections,
                )
                await execution.append(decision.events)
                if decision.action == ResponseAction.NEXT_STEP:
                    execution.corrections += 1
                    await self._end_step("protocol_correction")
                    continue
                if decision.action == ResponseAction.CANCELLED:
                    return await self._finalize(TurnStatus.CANCELLED, "Provider cancelled")
                if decision.action == ResponseAction.FAILED:
                    return await self._finalize(
                        TurnStatus.FAILED,
                        decision.error or "Provider terminal failure",
                    )
                if decision.action == ResponseAction.FINAL_CANDIDATE:
                    outcome = await self._finish_success(decision.final_content)
                    if outcome is not None:
                        return outcome
                    continue

                batch = await self.tools.execute(
                    decision.calls,
                    session_id=execution.session_id,
                    turn=execution.turn,
                    step=execution.step,
                    cancel=execution.cancel,
                    active_resources=frozenset(execution.resource_locks),
                    commit=execution.append,
                )
                if batch.issues:
                    rejection = reject_batch(
                        [issue.to_dict() for issue in batch.issues],
                        turn=execution.turn,
                        step=execution.step,
                        corrections=execution.corrections,
                        max_corrections=execution.context.config.max_protocol_corrections,
                    )
                    await execution.append(rejection.events)
                    if rejection.action == ResponseAction.NEXT_STEP:
                        execution.corrections += 1
                        await self._end_step("tool_batch_rejected")
                        continue
                    return await self._finalize(
                        TurnStatus.FAILED,
                        rejection.error or "Tool batch rejected",
                    )
                settled = settle_outcomes(
                    batch.outcomes,
                    turn=execution.turn,
                    step=execution.step,
                    pacing_step_interval=execution.context.config.pacing_step_interval,
                    blocked_resources=batch.blocked_resources,
                    timings=batch.timings,
                )
                await execution.append(settled.events)
                execution.resource_locks.update(settled.acquired_resources)
                execution.resource_locks.difference_update(settled.released_resources)
                if settled.action == BatchAction.BLOCKED:
                    return await self._finalize(
                        TurnStatus.BLOCKED,
                        settled.error or "Blocked by unresolved resources",
                    )
                if settled.action == BatchAction.CANCELLED or execution.cancel.is_set():
                    return await self._finalize(TurnStatus.CANCELLED, "Cancelled during Tool batch")
                if settled.action == BatchAction.FINALIZE:
                    outcome = await self._finish_success(settled.final_content)
                    if outcome is not None:
                        return outcome
                    continue
                await self._end_step("tool_batch_completed")
        except InvariantViolation:
            raise
        except Exception as exc:
            return await self._finalize(TurnStatus.FAILED, f"{type(exc).__name__}: {exc}")

    async def _begin_step(self) -> None:
        execution = self.execution
        async with execution.actor.commit_lock:
            execution.step += 1
            execution.open_step = True
            events = [
                NewEvent(event_type=EventType.STEP_START, turn=execution.turn, step=execution.step)
            ]
            inputs = []
            if execution.step == 1:
                inputs.append((execution.context.input_id, execution.context.initial_input))
            inputs.extend(execution.actor.inbox)
            execution.actor.inbox.clear()
            for input_id, content in inputs:
                events.append(
                    NewEvent(
                        event_type=EventType.USER_MESSAGE,
                        turn=execution.turn,
                        step=execution.step,
                        surface_op=SurfaceOp.APPEND,
                        payload={
                            "input_id": input_id,
                            "message": message_to_data(Message(role="user", content=content)),
                        },
                    )
                )
            await execution._append_locked(events)

    async def _end_step(self, status: str) -> None:
        execution = self.execution
        if not execution.open_step:
            return
        await execution.append(
            [
                NewEvent(
                    event_type=EventType.STEP_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"status": status},
                )
            ]
        )
        execution.open_step = False

    async def _end_step_locked(self, status: str) -> None:
        execution = self.execution
        if not execution.open_step:
            return
        await execution._append_locked(
            [
                NewEvent(
                    event_type=EventType.STEP_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"status": status},
                )
            ]
        )
        execution.open_step = False

    async def _working_context_expiry(self) -> NewEvent | None:
        execution = self.execution
        surface = await execution.context.store.load_surface(execution.session_id)
        active = latest_working_context(surface)
        if active is None:
            return None
        marker = active.provider_data.get("working_context", {})
        if marker.get("scope") != "turn":
            return None
        message = Message(role="user", content="[WorkingContext expired · turn]")
        return NewEvent(
            event_type=EventType.USER_MESSAGE,
            turn=execution.turn,
            step=execution.step,
            surface_op=SurfaceOp.APPEND,
            payload={
                "message": message_to_data(message),
                "working_context": {"action": "expired", "scope": "turn"},
            },
        )

    async def _finish_success(self, content: str) -> TurnOutcome | None:
        execution = self.execution
        async with execution.actor.commit_lock:
            if execution.actor.inbox:
                await self._end_step_locked("steer")
                return None
            if execution.cancel.is_set():
                raise _CancelFinalization()
            final_message_id = new_id("final")
            final = FinalMessage(
                final_message_id=final_message_id,
                session_id=execution.session_id,
                turn=execution.turn,
                step=execution.step,
                content=content,
            )
            events = [
                NewEvent(
                    event_type=EventType.FINAL_MESSAGE,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"final_message_id": final_message_id, "content": content},
                )
            ]
            expiry = await self._working_context_expiry()
            if expiry is not None:
                events.append(expiry)
            if execution.open_step:
                events.append(
                    NewEvent(
                        event_type=EventType.STEP_END,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"status": "final"},
                    )
                )
                execution.open_step = False
            events.append(
                NewEvent(
                    event_type=EventType.TURN_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={
                        "status": TurnStatus.SUCCEEDED.value,
                        "final_message_id": final_message_id,
                    },
                )
            )
            await execution._append_locked(events)
            execution.final_committed = True
        try:
            await execution.context.delivery.deliver(final)
            await execution.append(
                [
                    NewEvent(
                        event_type=EventType.DELIVERY_COMMITTED,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"final_message_id": final_message_id},
                    )
                ]
            )
            status = TurnStatus.SUCCEEDED
            error = None
        except _CancelFinalization:
            return await self._finalize(TurnStatus.CANCELLED, "Cancelled before final commitment")
        except Exception as exc:
            error = str(exc)
            await execution.append(
                [
                    NewEvent(
                        event_type=EventType.DELIVERY_UNKNOWN,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"final_message_id": final_message_id, "error": error},
                    )
                ]
            )
            status = TurnStatus.DELIVERY_UNKNOWN
        return TurnOutcome(
            status=status,
            session_id=execution.session_id,
            turn=execution.turn,
            step=execution.step,
            final_message_id=final_message_id,
            error=error,
        )

    async def _finalize(self, status: TurnStatus, error: str) -> TurnOutcome:
        execution = self.execution
        events = []
        expiry = await self._working_context_expiry()
        if expiry is not None:
            events.append(expiry)
        if execution.open_step:
            events.append(
                NewEvent(
                    event_type=EventType.STEP_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"status": status.value},
                )
            )
            execution.open_step = False
        events.append(
            NewEvent(
                event_type=EventType.TURN_END,
                turn=execution.turn,
                step=execution.step,
                payload={"status": status.value, "error": error},
            )
        )
        await execution.append(events)
        return TurnOutcome(
            status=status,
            session_id=execution.session_id,
            turn=execution.turn,
            step=execution.step,
            error=error,
        )
