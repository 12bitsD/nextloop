from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .adapters.capabilities import HistoryCheck
from .contracts import (
    EVENT_SCHEMA_VERSION,
    Cancelled,
    Completed,
    Event,
    EventStore,
    EventType,
    FinalMessage,
    InvariantViolation,
    Message,
    ModelUsage,
    NewEvent,
    Skipped,
    SurfaceOp,
    ToolCall,
    ToolEffect,
    TurnContext,
    TurnOutcome,
    TurnStatus,
    UncertainHanging,
    new_id,
)
from .events.approval import approval_decision_event, approval_expired, pending_approval
from .events.codec import is_surface_event, is_uncertain_result, with_origin
from .events.input import user_input_events
from .events.outcome import BatchAction, reconciliation_event, settle_outcomes
from .events.project import (
    compaction_events,
    latest_working_context,
    make_compaction_request,
    message_to_data,
    project_events,
    valid_compaction,
)
from .events.repair import active_resource_locks
from .events.response import ResponseAction, decide_response, reject_batch
from .execute.batch import BatchExecutor
from .execute.cancellation import await_with_cancel
from .execute.cleanup import wait_for_cleanup
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
    surface: list[Event] = field(default_factory=list)
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
            await self.append_locked(events)

    async def append_locked(self, events: Sequence[NewEvent]) -> None:
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
                self.surface = [event]
            elif is_surface_event(event.event_type, event.surface_op):
                self.surface.append(event)

    async def has_steer(self) -> bool:
        async with self.actor.commit_lock:
            return bool(self.actor.inbox)

    async def request_cancel(self, cause: str) -> bool:
        async with self.actor.commit_lock:
            if self.cancel_cause is not None:
                return False
            if self.final_committed and cause != "turn_deadline_exceeded":
                return False
            if getattr(self.context.store, "lease_seconds", None) is not None:
                try:
                    await self.context.store.check_writer(self.session_id, self.writer_epoch)
                except InvariantViolation:
                    return False
            await self.append_locked(
                [
                    NewEvent(
                        EventType.CANCELLATION_REQUESTED,
                        turn=self.turn,
                        step=self.step,
                        payload={"cause": cause},
                    )
                ]
            )
            self.cancel_cause = cause
            self.cancel.set()
            return True


class ExecutionFrame:
    def __init__(
        self,
        context: TurnContext,
        actor: SessionActor,
        active: dict[str, SessionExecution],
        stores: dict[str, EventStore],
    ) -> None:
        self.context = context
        self.actor = actor
        self._active = active
        self._stores = stores
        self.history: Sequence[Event] = ()
        self.execution: SessionExecution | None = None
        self.writer_epoch = 0
        self.last_seq = 0
        self.last_turn = 0

    async def __aenter__(self) -> ExecutionFrame:
        if not self.context.input_id.strip():
            raise ValueError("input_id must be non-empty")
        session_id = self.context.session_id
        self._stores[session_id] = self.context.store
        await self.actor.run_lock.acquire()
        try:
            self.history = await self.context.store.load_recovery(session_id)
            return self
        except BaseException:
            self.actor.run_lock.release()
            raise

    async def activate(self, check: HistoryCheck | None = None) -> SessionExecution:
        checked_attach = getattr(self.context.store, "attach_writer_checked", None)
        if check is not None and callable(checked_attach):
            (
                self.writer_epoch,
                self.last_seq,
                self.last_turn,
                self.history,
            ) = await checked_attach(self.context.session_id, check)
        else:
            (
                self.writer_epoch,
                self.last_seq,
                self.last_turn,
            ) = await self.context.store.attach_writer(self.context.session_id)
        context = self.context
        execution = SessionExecution(
            context=context,
            actor=self.actor,
            writer_epoch=self.writer_epoch,
            seq=self.last_seq,
            turn=self.last_turn + 1,
            resource_locks=set(active_resource_locks(self.history)),
        )
        self.execution = execution
        execution.surface = list(await context.store.load_surface(context.session_id))
        execution.accepted_inputs = {
            str(event.payload["input_id"]): str(event.payload.get("content", ""))
            for event in self.history
            if event.event_type == EventType.INPUT_ACCEPTED and event.payload.get("input_id")
        }
        if (
            context.input_id in execution.accepted_inputs
            and execution.accepted_inputs[context.input_id] != context.initial_input
        ):
            raise InvariantViolation("input_id reused with different content")
        from .events.pending import pending_inputs

        execution.recovered_inputs.extend(
            (item.input_id, item.content) for item in pending_inputs(self.history)
        )
        if context.preview is not None:
            execution.preview = PreviewChannel(context.preview)
        self._active[context.session_id] = execution
        deadline = context.config.turn_deadline_seconds
        if deadline is not None:
            execution.deadline_task = asyncio.create_task(self._expire(execution, deadline))
        interval = getattr(context.store, "lease_seconds", None)
        if interval is not None:
            execution.heartbeat_task = asyncio.create_task(self._heartbeat(execution, interval / 3))
        return execution

    async def begin(self) -> None:
        execution = self._require_execution()
        await execution.append(
            [
                NewEvent(
                    event_type=EventType.INPUT_ACCEPTED,
                    turn=execution.turn,
                    step=0,
                    payload={
                        "input_id": self.context.input_id,
                        "content": self.context.initial_input,
                    },
                ),
                NewEvent(event_type=EventType.TURN_START, turn=execution.turn, step=0),
            ]
        )

    async def begin_step(self) -> None:
        execution = self._require_execution()
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
            await execution.append_locked(events)

    async def end_step(self, status: str) -> None:
        execution = self._require_execution()
        if not execution.open_step:
            return
        await execution.append(
            [
                NewEvent(
                    EventType.STEP_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"status": status},
                )
            ]
        )
        execution.open_step = False

    async def end_step_locked(self, status: str) -> None:
        execution = self._require_execution()
        if not execution.open_step:
            return
        await execution.append_locked(
            [
                NewEvent(
                    EventType.STEP_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={"status": status},
                )
            ]
        )
        execution.open_step = False

    def _require_execution(self) -> SessionExecution:
        if self.execution is None:
            raise RuntimeError("ExecutionFrame is not active")
        return self.execution

    async def _expire(self, execution: SessionExecution, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await execution.request_cancel("turn_deadline_exceeded")

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

    async def _cleanup(self, execution: SessionExecution) -> None:
        tasks = [execution.deadline_task, execution.heartbeat_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        try:
            if execution.preview is not None:
                await execution.preview.close()
        finally:
            self._active.pop(execution.session_id, None)

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self.execution is not None:
                cleanup = asyncio.create_task(self._cleanup(self.execution))
                if await wait_for_cleanup(cleanup):
                    raise asyncio.CancelledError()
        finally:
            self.actor.run_lock.release()


class _CancelFinalization(Exception):
    pass


class _DeliveryCancelled(Exception):
    pass


class TurnFinalizer:
    def __init__(self, frame: ExecutionFrame) -> None:
        self.frame = frame
        self.execution = frame._require_execution()

    async def _working_context_expiry(self) -> NewEvent | None:
        execution = self.execution
        active = latest_working_context(execution.surface)
        if active is None:
            return None
        marker = active.provider_data.get("working_context", {})
        if marker.get("scope") != "turn":
            return None
        return NewEvent(
            event_type=EventType.USER_MESSAGE,
            turn=execution.turn,
            step=execution.step,
            surface_op=SurfaceOp.APPEND,
            payload={
                "message": message_to_data(
                    Message(role="user", content="[WorkingContext expired · turn]")
                ),
                "working_context": {"action": "expired", "scope": "turn"},
            },
        )

    async def _commit_terminal_locked(self, events: Sequence[NewEvent]) -> None:
        await self.execution.append_locked(events)
        self.execution.open_step = False
        self.execution.final_committed = True

    async def finish_success(self, content: str) -> TurnOutcome | None:
        execution = self.execution
        async with execution.actor.commit_lock:
            if execution.actor.inbox:
                await self.frame.end_step_locked("steer")
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
                        EventType.STEP_END,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"status": "final"},
                    )
                )
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
            await self._commit_terminal_locked(events)
        try:
            await await_with_cancel(
                execution.context.delivery.deliver(final), execution.cancel, _DeliveryCancelled
            )
            await execution.append(
                [
                    NewEvent(
                        EventType.DELIVERY_COMMITTED,
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
                        EventType.DELIVERY_UNKNOWN,
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
                        EventType.DELIVERY_UNKNOWN,
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

    async def finalize(
        self,
        status: TurnStatus,
        error: str | None,
        *,
        facts: Sequence[NewEvent] = (),
    ) -> TurnOutcome:
        execution = self.execution
        async with execution.actor.commit_lock:
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
                        EventType.STEP_END,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"status": status.value},
                    )
                )
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
            await self._commit_terminal_locked(events)
        return TurnOutcome(
            status=status,
            session_id=execution.session_id,
            turn=execution.turn,
            step=execution.step,
            error=error,
        )

    async def approval_pending(self, approval_id: str) -> TurnOutcome:
        execution = self.execution
        async with execution.actor.commit_lock:
            events = []
            if execution.open_step:
                events.append(
                    NewEvent(
                        EventType.STEP_END,
                        turn=execution.turn,
                        step=execution.step,
                        payload={"status": TurnStatus.APPROVAL_PENDING.value},
                    )
                )
            events.append(
                NewEvent(
                    EventType.TURN_END,
                    turn=execution.turn,
                    step=execution.step,
                    payload={
                        "status": TurnStatus.APPROVAL_PENDING.value,
                        "approval_id": approval_id,
                    },
                )
            )
            await self._commit_terminal_locked(events)
        return TurnOutcome(
            status=TurnStatus.APPROVAL_PENDING,
            session_id=execution.session_id,
            turn=execution.turn,
            step=execution.step,
            error=f"Approval pending: {approval_id}",
        )

    async def budget_failure(self, reason: str) -> TurnOutcome:
        self.execution.terminal_reason = (
            reason
            if reason in ("budget_usage_unavailable", "budget_usage_invalid")
            else "budget_exceeded"
        )
        return await self.finalize(TurnStatus.FAILED, reason)


class TurnDriver:
    def __init__(self, frame: ExecutionFrame) -> None:
        self.frame = frame
        self.execution = frame._require_execution()
        self.finalizer = TurnFinalizer(frame)
        context = self.execution.context
        self.model = ModelCaller(
            context.provider,
            context.config,
            self.execution.has_steer,
            before_attempt=self.execution.before_model_attempt,
            record_usage=self.execution.record_usage,
            preview=self.execution.preview,
        )
        self.tools = BatchExecutor(
            context.tools,
            context.config.max_parallel_tools,
            admit=self.execution.admit_tool_batch,
        )

    async def run(self) -> TurnOutcome:
        execution = self.execution
        try:
            while True:
                if execution.cancel.is_set():
                    return await self.finalizer.finalize(TurnStatus.CANCELLED, "Cancelled")
                reason = execution.step_budget_reason()
                if reason is not None:
                    return await self.finalizer.budget_failure(reason)
                await self.frame.begin_step()
                projection = project_events(
                    execution.surface,
                    session_id=execution.session_id,
                    turn=execution.turn,
                    step=execution.step,
                    config=execution.context.config,
                    tools=execution.context.tools.catalog(),
                )
                if projection.compaction_reason is not None:
                    surface = tuple(execution.surface)
                    compaction = await self.model.call(
                        make_compaction_request(projection, execution.context.config),
                        execution.cancel,
                        purpose="compaction",
                    )
                    await execution.append(compaction.events)
                    if compaction.budget_reason is not None:
                        return await self.finalizer.budget_failure(compaction.budget_reason)
                    if compaction.rebuild_for_steer:
                        await self.frame.end_step("steer")
                        continue
                    if compaction.cancelled or execution.cancel.is_set():
                        return await self.finalizer.finalize(
                            TurnStatus.CANCELLED, "Cancelled during compaction"
                        )
                    summary = compaction.response.content if compaction.response else ""
                    if compaction.error or not valid_compaction(
                        summary,
                        projection,
                        bootstrap_count=len(execution.context.config.bootstrap_messages),
                    ):
                        error = str(
                            compaction.error
                            or "Compaction returned an empty or non-shrinking summary"
                        )
                        return await self.finalizer.finalize(TurnStatus.FAILED, error)
                    await execution.append(
                        compaction_events(
                            summary,
                            surface,
                            turn=execution.turn,
                            step=execution.step,
                        )
                    )
                    projection = project_events(
                        execution.surface,
                        session_id=execution.session_id,
                        turn=execution.turn,
                        step=execution.step,
                        config=execution.context.config,
                        tools=execution.context.tools.catalog(),
                    )
                    if projection.compaction_reason is not None:
                        return await self.finalizer.finalize(
                            TurnStatus.FAILED,
                            "Request still exceeds capacity after one compaction",
                        )

                result = await self.model.call(projection.request, execution.cancel)
                await execution.append(result.events)
                if result.budget_reason is not None:
                    return await self.finalizer.budget_failure(result.budget_reason)
                if result.rebuild_for_steer:
                    await self.frame.end_step("steer")
                    continue
                if result.cancelled or execution.cancel.is_set():
                    return await self.finalizer.finalize(TurnStatus.CANCELLED, "Cancelled")
                if result.error or result.response is None:
                    return await self.finalizer.finalize(
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
                    await self.frame.end_step("protocol_correction")
                    continue
                if decision.action == ResponseAction.CANCELLED:
                    return await self.finalizer.finalize(TurnStatus.CANCELLED, "Provider cancelled")
                if decision.action == ResponseAction.FAILED:
                    return await self.finalizer.finalize(
                        TurnStatus.FAILED,
                        decision.error or "Provider terminal failure",
                    )
                if decision.action == ResponseAction.FINAL_CANDIDATE:
                    outcome = await self.finalizer.finish_success(decision.final_content)
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
                    return await self.finalizer.approval_pending(batch.approval.approval_id)
                if batch.budget_reason is not None:
                    return await self.finalizer.budget_failure(batch.budget_reason)
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
                        await self.frame.end_step("tool_batch_rejected")
                        continue
                    return await self.finalizer.finalize(
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
                    return await self.finalizer.finalize(
                        TurnStatus.FAILED,
                        settled.error or "Tool result failed",
                    )
                if settled.action == BatchAction.BLOCKED:
                    return await self.finalizer.finalize(
                        TurnStatus.BLOCKED,
                        settled.error or "Blocked by unresolved resources",
                    )
                if settled.action == BatchAction.CANCELLED or execution.cancel.is_set():
                    return await self.finalizer.finalize(
                        TurnStatus.CANCELLED, "Cancelled during Tool batch"
                    )
                if settled.action == BatchAction.FINALIZE:
                    outcome = await self.finalizer.finish_success(settled.final_content)
                    if outcome is not None:
                        return outcome
                    continue
                await self.frame.end_step("tool_batch_completed")
        except _CancelFinalization:
            return await self.finalizer.finalize(
                TurnStatus.CANCELLED, "Cancelled before final commitment"
            )
        except InvariantViolation:
            raise
        except Exception as exc:
            return await self.finalizer.finalize(TurnStatus.FAILED, f"{type(exc).__name__}: {exc}")


async def run_approval_resume(
    frame: ExecutionFrame,
    *,
    decisions: Mapping[str, bool] | None,
    reject_all: bool,
) -> TurnOutcome:
    context = frame.context
    history = frame.history
    request = pending_approval(history)
    if request is None:
        raise InvariantViolation("No pending approval")
    expected = {item.operation_id for item in request.items}
    if reject_all:
        resolved = {operation_id: False for operation_id in expected}
    else:
        if decisions is None or set(decisions) != expected:
            raise InvariantViolation("Approval decisions must cover every original operation")
        resolved = dict(decisions)
    if any(type(value) is not bool for value in resolved.values()):
        raise InvariantViolation("Approval decisions must be boolean")
    if any(
        event.event_type == EventType.TOOL_CALL and event.payload.get("operation_id") in expected
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
            if spec is None or item.schema_version != EVENT_SCHEMA_VERSION or not policy_matches:
                raise InvariantViolation(
                    "Approval policy or Event schema changed; reload decisions"
                )

    def validate_current(events: Sequence[Event]) -> None:
        current = pending_approval(events)
        if current is None:
            raise InvariantViolation("No pending approval")
        if (
            current.approval_id != request.approval_id
            or {item.operation_id for item in current.items} != expected
        ):
            raise InvariantViolation("Approval changed; reload decisions")
        if any(
            event.event_type == EventType.TOOL_CALL
            and event.payload.get("operation_id") in expected
            for event in events
        ):
            raise InvariantViolation("Approval dispatch already started; recover first")

    execution = await frame.activate(validate_current)
    await frame.begin()
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
                    with_origin(event, request.origin_turn, request.origin_step, "approval")
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
                context.session_id, execution.writer_epoch
            ),
            approval_checked=True,
        )
        if batch.budget_reason:
            return await TurnFinalizer(frame).budget_failure(batch.budget_reason)
        if batch.issues:
            raise InvariantViolation("Approved batch could not be admitted")
        outcomes = batch.outcomes
        blocked = batch.blocked_resources
        timings = batch.timings
    else:
        validated, issues = context.tools.validate(calls)
        if issues:
            raise InvariantViolation("; ".join(issue.message for issue in issues))
        outcomes = tuple(
            (item.call, item.spec, Skipped(decision_cause or "Rejected by approval"))
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
        with_origin(event, request.origin_turn, request.origin_step, "approval")
        if event.event_type == EventType.TOOL_RESULT
        else event
        for event in settled.events
    ]
    execution.resource_locks.update(settled.acquired_resources)
    execution.resource_locks.difference_update(settled.released_resources)
    finalizer = TurnFinalizer(frame)
    terminal = {
        BatchAction.BLOCKED: TurnStatus.BLOCKED,
        BatchAction.CANCELLED: TurnStatus.CANCELLED,
        BatchAction.FAILED: TurnStatus.FAILED,
    }
    if settled.action in terminal:
        return await finalizer.finalize(terminal[settled.action], settled.error, facts=events)
    if settled.action == BatchAction.FINALIZE:
        await execution.append(events)
        outcome = await finalizer.finish_success(settled.final_content)
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
    return await TurnDriver(frame).run()


def has_successful_reconciliation(events: Sequence[Event], operation_id: str) -> bool:
    return any(
        event.event_type == EventType.RECONCILIATION_RESULT
        and event.payload.get("operation_id") == operation_id
        and event.payload.get("outcome", {}).get("kind") == "completed"
        and not event.payload.get("outcome", {}).get("is_error", False)
        for event in events
    )


async def run_reconciliation(frame: ExecutionFrame, operation_id: str) -> TurnOutcome:
    context = frame.context
    history = frame.history
    original_result = next(
        (
            event
            for event in history
            if event.event_type == EventType.TOOL_RESULT
            and event.payload.get("operation_id") == operation_id
            and is_uncertain_result(event)
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
    if has_successful_reconciliation(history, operation_id):
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

    def validate_current(events: Sequence[Event]) -> None:
        if not any(
            event.event_type == EventType.TOOL_RESULT
            and event.payload.get("operation_id") == operation_id
            and is_uncertain_result(event)
            for event in events
        ):
            raise InvariantViolation("Operation must have a recovered uncertain result")
        if has_successful_reconciliation(events, operation_id):
            raise InvariantViolation("Operation is already reconciled")

    execution = await frame.activate(validate_current)
    await frame.begin()
    execution.step = 1
    execution.open_step = True
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
                    "message": message_to_data(Message(role="user", content=context.initial_input)),
                },
            ),
        ]
    )

    async def commit(events: Sequence[NewEvent]) -> None:
        await execution.append(
            [with_origin(event, original.turn, original.step) for event in events]
        )

    batch = await BatchExecutor(
        context.tools,
        context.config.max_parallel_tools,
        admit=execution.admit_tool_batch,
    ).execute(
        [call],
        session_id=context.session_id,
        turn=execution.turn,
        step=1,
        cancel=execution.cancel,
        active_resources=frozenset(),
        commit=commit,
        check_writer=lambda: context.store.check_writer(context.session_id, execution.writer_epoch),
    )
    finalizer = TurnFinalizer(frame)
    if batch.budget_reason:
        return await finalizer.budget_failure(batch.budget_reason)
    if batch.issues or len(batch.outcomes) != 1:
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
                    *original_result.payload.get("outcome", {}).get("resource_locks", ()),
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
    return await finalizer.finalize(status, error, facts=[reconciliation])
