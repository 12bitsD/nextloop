from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..contracts import (
    CandidateRequest,
    CapacityError,
    Event,
    EventType,
    InvariantViolation,
    LoopConfig,
    Message,
    ModelUsage,
    NewEvent,
    SurfaceOp,
    ToolSpec,
)
from .codec import fold_projection_facts, message_from_data
from .codec import message_to_data as message_to_data


def usage_to_data(usage: ModelUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
    }


@dataclass(frozen=True)
class Projection:
    request: CandidateRequest
    compaction_reason: str | None
    working_context: Message | None


def make_working_context_message(
    content: str, *, scope: Literal["turn", "session"] = "turn"
) -> Message:
    if scope not in ("turn", "session"):
        raise ValueError("WorkingContext scope must be 'turn' or 'session'")
    content = content.strip()
    if not content:
        raise ValueError("WorkingContext content must be non-empty")
    return Message(
        role="user",
        content=f"[WorkingContext · {scope}]\n{content}",
        provider_data={"working_context": {"action": "set", "scope": scope}},
    )


def make_clear_working_context_message(*, scope: Literal["turn", "session"] = "turn") -> Message:
    if scope not in ("turn", "session"):
        raise ValueError("WorkingContext scope must be 'turn' or 'session'")
    return Message(
        role="user",
        content=f"[WorkingContext cleared · {scope}]",
        provider_data={"working_context": {"action": "clear", "scope": scope}},
    )


def latest_working_context(events: Sequence[Event]) -> Message | None:
    return fold_projection_facts(events).working_context


def _capacity_reason(request_bytes: int, config: LoopConfig) -> str | None:
    estimated_input = max(1, (request_bytes + 3) // 4)
    if estimated_input >= config.context_window * 0.75:
        return "input_threshold_75_percent"
    if estimated_input + config.output_reserve > config.context_window:
        return "input_output_capacity"
    return None


def project_events(
    events: Sequence[Event],
    *,
    session_id: str,
    turn: int,
    step: int,
    config: LoopConfig,
    tools: Sequence[ToolSpec],
) -> Projection:
    facts = fold_projection_facts(events)
    rejected_steps = facts.rejected_steps
    pending = [Message(role="user", content=item.content) for item in facts.pending]
    approval_result_seq = facts.approval_result_seq
    injected = False
    messages = [Message(role="system", content=config.system_prompt)]
    messages.extend(config.bootstrap_messages)
    for event in events:
        if (
            not injected
            and approval_result_seq == 0
            and event.event_type == EventType.USER_MESSAGE
            and event.turn == turn
            and event.step == 1
        ):
            messages.extend(pending)
            injected = True
        if event.event_type == EventType.COMPACTION:
            messages.append(
                Message(role="user", content="[Task summary]\n" + str(event.payload["summary"]))
            )
        elif event.event_type in (
            EventType.USER_MESSAGE,
            EventType.ASSISTANT_MESSAGE,
            EventType.TOOL_RESULT,
            EventType.RECONCILIATION_RESULT,
        ):
            message = message_from_data(event.payload["message"])
            if (
                event.event_type == EventType.ASSISTANT_MESSAGE
                and (event.turn, event.step) in rejected_steps
            ):
                if not message.content:
                    continue
                message = Message(
                    role=message.role,
                    content=message.content,
                    provider_data=message.provider_data,
                )
            messages.append(message)
            if not injected and event.seq == approval_result_seq:
                messages.extend(pending)
                injected = True
    if not injected:
        messages.extend(pending)
    request = CandidateRequest(
        session_id=session_id,
        turn=turn,
        step=step,
        messages=tuple(messages),
        tools=tuple(tools),
    )
    reason = _capacity_reason(len(request.canonical_bytes()), config)
    return Projection(
        request=request,
        compaction_reason=reason,
        working_context=facts.working_context,
    )


def make_compaction_request(projection: Projection, config: LoopConfig) -> CandidateRequest:
    if not config.compaction_prompt.strip():
        raise CapacityError("Compaction is required but compaction_prompt is empty")
    request = projection.request
    return CandidateRequest(
        session_id=request.session_id,
        turn=request.turn,
        step=request.step,
        messages=(
            Message(role="system", content=config.compaction_prompt),
            *config.bootstrap_messages,
            *request.messages[1 + len(config.bootstrap_messages) :],
        ),
        tools=(),
    )


def _message_bytes(message: Message) -> int:
    return len(
        json.dumps(
            message_to_data(message), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def valid_compaction(summary: str, projection: Projection, *, bootstrap_count: int = 0) -> bool:
    summary = summary.strip()
    if not summary:
        return False
    replaced = projection.request.messages[1 + bootstrap_count :]
    before_bytes = sum(_message_bytes(message) for message in replaced)
    retained = (Message(role="user", content="[Task summary]\n" + summary),) + (
        (projection.working_context,) if projection.working_context is not None else ()
    )
    after_bytes = sum(_message_bytes(message) for message in retained)
    return after_bytes < before_bytes


def _require_paired_tool_intents(events: Sequence[Event]) -> None:
    rejected = {
        (event.turn, event.step)
        for event in events
        if event.event_type == EventType.TOOL_BATCH_REJECTED
    }
    result_operations = {
        str(event.payload.get("operation_id"))
        for event in events
        if event.event_type == EventType.TOOL_RESULT and event.payload.get("operation_id")
    }
    result_coordinates = {
        (event.turn, event.step, str(event.payload.get("call_id")))
        for event in events
        if event.event_type == EventType.TOOL_RESULT
    }
    for event in events:
        if event.event_type != EventType.ASSISTANT_MESSAGE or (event.turn, event.step) in rejected:
            continue
        for call in event.payload["message"].get("tool_calls", ()):
            operation_id = call.get("operation_id")
            if operation_id and str(operation_id) in result_operations:
                continue
            coordinate = (event.turn, event.step, str(call["call_id"]))
            if coordinate not in result_coordinates:
                raise InvariantViolation("Cannot compact across an unresolved Tool Intent")


def compaction_events(
    summary: str,
    surface_events: Sequence[Event],
    *,
    turn: int,
    step: int,
) -> tuple[NewEvent, ...]:
    _require_paired_tool_intents(surface_events)
    events = [
        NewEvent(
            event_type=EventType.COMPACTION,
            turn=turn,
            step=step,
            surface_op=SurfaceOp.EPOCH,
            payload={
                "summary": summary.strip(),
                "source_seqs": (
                    [[surface_events[0].seq, surface_events[-1].seq]] if surface_events else []
                ),
            },
        )
    ]
    working_context = latest_working_context(surface_events)
    if working_context is not None:
        marker = working_context.provider_data.get(
            "working_context", {"action": "set", "scope": "session"}
        )
        events.append(
            NewEvent(
                event_type=EventType.USER_MESSAGE,
                turn=turn,
                step=step,
                surface_op=SurfaceOp.APPEND,
                payload={
                    "message": message_to_data(working_context),
                    "working_context": marker,
                },
            )
        )
    return tuple(events)
