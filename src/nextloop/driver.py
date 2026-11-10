from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from .contracts import (
    Event,
    EventType,
    FinalMessage,
    InvariantViolation,
    Message,
    ModelUsage,
    NewEvent,
    SurfaceOp,
    TurnContext,
    TurnOutcome,
    TurnStatus,
    new_id,
)
from .events.input import user_input_events
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
from .execute.cancellation import await_with_cancel
from .execute.model import ModelCaller
from .execute.preview import PreviewChannel


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
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cancel_cause: str | None = None
    terminal_reason: str | None = None
    deadline_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    surface: tuple[Event, ...] = ()
    recovered_inputs: list[tuple[str, str]] = field(default_factory=list)
    accepted_inputs: dict[str, str] = field(default_factory=dict)
    preview: PreviewChannel | None = None

    @property
    def session_id(self) -> str:
        return self.context.session_id

    @property
    def cancel(self) -> asyncio.Event:
        if not hasattr(self, "_cancel"):
            self._cancel = asyncio.Event()
        return self._cancel

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def budget_snapshot(self) -> dict[str, int]:
        return {
            "steps": self.step,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }

    def step_budget_reason(self) -> str | None:
        limit = self.context.config.max_steps
        if limit is not None and self.step >= limit:
            return f"max_steps={limit}"
        return None

    def before_model_attempt(self) -> str | None:
        limit = self.context.config.max_model_calls
        if limit is not None and self.model_calls >= limit:
            return f"max_model_calls={limit}"
        self.model_calls += 1
        return None

    def admit_tool_batch(self, count: int) -> str | None:
        limit = self.context.config.max_tool_calls
        if limit is not None and self.tool_calls + count > limit:
            return f"max_tool_calls={limit}"
        self.tool_calls += count
        return None

    def record_usage(self, usage: ModelUsage | None) -> str | None:
        config = self.context.config
        limits = (
            config.max_input_tokens,
            config.max_output_tokens,
            config.max_total_tokens,
        )
        if all(limit is None for limit in limits):
            return None
        if usage is None:
            return "budget_usage_unavailable"
        for value in (
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_input_tokens,
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                return "budget_usage_invalid"
        if config.max_input_tokens is not None and usage.input_tokens is None:
            return "budget_usage_unavailable"
        if config.max_output_tokens is not None and usage.output_tokens is None:
            return "budget_usage_unavailable"
        if config.max_total_tokens is not None and (
            usage.input_tokens is None or usage.output_tokens is None
        ):
            return "budget_usage_unavailable"
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        for name, value, limit in (
            ("max_input_tokens", self.input_tokens, config.max_input_tokens),
            ("max_output_tokens", self.output_tokens, config.max_output_tokens),
            ("max_total_tokens", self.total_tokens, config.max_total_tokens),
        ):
            if limit is not None and value > limit:
                return f"{name}={limit}"
        return None

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
        previous_seq = self.seq
        self.seq = committed[-1].seq
        if self.seq <= previous_seq:
            return
        for event in committed:
            if event.event_type == EventType.INPUT_ACCEPTED:
                self.accepted_inputs[str(event.payload["input_id"])] = str(
                    event.payload.get("content", "")
                )
            if event.surface_op == SurfaceOp.EPOCH:
                self.surface = (event,)
            elif event.surface_op == SurfaceOp.APPEND or event.event_type in (
                EventType.TOOL_BATCH_REJECTED,
                EventType.INPUT_ACCEPTED,
            ):
                self.surface = (*self.surface, event)

    async def has_steer(self) -> bool:
        async with self.actor.commit_lock:
            return bool(self.actor.inbox)


class _CancelFinalization(Exception):
    pass


class _DeliveryCancelled(Exception):
    pass


class TurnDriver:
    def __init__(self, execution: SessionExecution) -> None:
        self.execution = execution
        context = execution.context
        if context.preview is not None:
            execution.preview = PreviewChannel(context.preview)
        self.model = ModelCaller(
            context.provider,
            context.config,
            execution.has_steer,
            before_attempt=execution.before_model_attempt,
            record_usage=execution.record_usage,
            preview=execution.preview,
        )
        self.tools = BatchExecutor(
            context.tools, context.config.max_parallel_tools, admit=execution.admit_tool_batch
        )

    async def run(self) -> TurnOutcome:
        execution = self.execution
        try:
            while True:
                if execution.cancel.is_set():
                    return await self._finalize(TurnStatus.CANCELLED, "Cancelled")
                reason = execution.step_budget_reason()
                if reason is not None:
                    return await self._budget_failure(reason)
                await self._begin_step()
                surface = execution.surface
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
                    if compaction.budget_reason is not None:
                        return await self._budget_failure(compaction.budget_reason)
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
                    surface = execution.surface
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
                if result.budget_reason is not None:
                    return await self._budget_failure(result.budget_reason)
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
                if decision.action != ResponseAction.RUN_TOOLS:
                    await execution.append(decision.events)
                if execution.preview is not None:
                    execution.preview.resolved(
                        projection.request,
                        attempt,
                        action=decision.action.value,
                        decision_event_ids=tuple(item.event_id for item in decision.events),
                    )
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
                    check_writer=lambda: execution.context.store.check_writer(
                        execution.session_id, execution.writer_epoch
                    ),
                    intent_events=decision.events,
                )
                if batch.approval is not None:
                    return await self._approval_pending(batch.approval.approval_id)
                if batch.budget_reason is not None:
                    return await self._budget_failure(batch.budget_reason)
                if batch.issues:
                    await execution.append(decision.events)
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
                    result_bytes=execution.context.config.tool_result_bytes,
                    full_max_bytes=execution.context.config.tool_result_full_max_bytes,
                )
                await execution.append(settled.events)
                execution.resource_locks.update(settled.acquired_resources)
                execution.resource_locks.difference_update(settled.released_resources)
                if settled.action == BatchAction.FAILED:
                    return await self._finalize(
                        TurnStatus.FAILED,
                        settled.error or "Tool result failed",
                    )
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
        except _CancelFinalization:
            return await self._finalize(TurnStatus.CANCELLED, "Cancelled before final commitment")
        except InvariantViolation:
            raise
        except Exception as exc:
            return await self._finalize(TurnStatus.FAILED, f"{type(exc).__name__}: {exc}")

    async def _approval_pending(self, approval_id: str) -> TurnOutcome:
        execution = self.execution
        await execution.append(
            [
                NewEvent(
                    EventType.STEP_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"status": TurnStatus.APPROVAL_PENDING.value},
                ),
                NewEvent(
                    EventType.TURN_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={
                        "status": TurnStatus.APPROVAL_PENDING.value,
                        "approval_id": approval_id,
                    },
                ),
            ]
        )
        execution.open_step = False
        return TurnOutcome(
            status=TurnStatus.APPROVAL_PENDING,
            session_id=execution.session_id,
            turn=execution.turn,
            step=execution.step,
            error=f"Approval pending: {approval_id}",
        )

    async def _budget_failure(self, reason: str) -> TurnOutcome:
        self.execution.terminal_reason = (
            reason
            if reason in ("budget_usage_unavailable", "budget_usage_invalid")
            else "budget_exceeded"
        )
        return await self._finalize(TurnStatus.FAILED, reason)

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
                inputs.extend(execution.recovered_inputs)
                inputs.append((execution.context.input_id, execution.context.initial_input))
                execution.recovered_inputs.clear()
            inputs.extend(execution.actor.inbox)
            execution.actor.inbox.clear()
            events.extend(user_input_events(inputs, turn=execution.turn, step=execution.step))
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
        surface = execution.surface
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
            await await_with_cancel(
                execution.context.delivery.deliver(final), execution.cancel, _DeliveryCancelled
            )
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
        except _DeliveryCancelled:
            error = "turn_deadline_exceeded"
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

    async def _finalize(
        self,
        status: TurnStatus,
        error: str | None,
        *,
        facts: Sequence[NewEvent] = (),
    ) -> TurnOutcome:
        execution = self.execution
        if execution.cancel_cause == "turn_deadline_exceeded":
            status = TurnStatus.FAILED
            error = "turn_deadline_exceeded"
            execution.terminal_reason = error
        events = list(facts)
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
        payload: dict[str, object] = {"status": status.value, "error": error}
        if execution.terminal_reason is not None:
            payload.update(
                reason=execution.terminal_reason,
                budget=execution.budget_snapshot(),
            )
        events.append(
            NewEvent(
                event_type=EventType.TURN_END,
                turn=execution.turn,
                step=execution.step,
                payload=payload,
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
