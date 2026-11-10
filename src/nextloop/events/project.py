from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
    ToolCall,
    ToolSpec,
)
from .pending import pending_inputs


def message_to_data(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "operation_id": call.operation_id,
            }
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "provider_data": dict(message.provider_data),
    }


def message_from_data(data: Mapping[str, Any]) -> Message:
    return Message(
        role=str(data["role"]),
        content=str(data.get("content", "")),
        tool_calls=tuple(
            ToolCall(
                call_id=str(call["call_id"]),
                name=str(call["name"]),
                arguments=str(call["arguments"]),
                operation_id=call.get("operation_id"),
            )
            for call in data.get("tool_calls", ())
        ),
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        provider_data=data.get("provider_data", {}),
    )


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


def latest_working_context(events: Sequence[Event]) -> Message | None:
    active: Message | None = None
    for event in events:
        if event.event_type != EventType.USER_MESSAGE:
            continue
        marker = event.payload.get("working_context")
        if not isinstance(marker, Mapping):
            continue
        action = marker.get("action")
        if action == "set":
            active = message_from_data(event.payload["message"])
        elif action in ("clear", "expired"):
            active = None
    return active


def project_events(
    events: Sequence[Event],
    *,
    session_id: str,
    turn: int,
    step: int,
    config: LoopConfig,
    tools: Sequence[ToolSpec],
) -> Projection:
    rejected_steps = {
        (event.turn, event.step)
        for event in events
        if event.event_type == EventType.TOOL_BATCH_REJECTED
    }
    pending = [Message(role="user", content=item.content) for item in pending_inputs(events)]
    approval_result_seq = max(
        (
            event.seq
            for event in events
            if event.event_type == EventType.TOOL_RESULT
            and event.payload.get("origin_kind") == "approval"
        ),
        default=0,
    )
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
    estimated_input = max(1, (len(request.canonical_bytes()) + 3) // 4)
    reason = None
    if estimated_input >= config.context_window * 0.75:
        reason = "input_threshold_75_percent"
    elif estimated_input + config.output_reserve > config.context_window:
        reason = "input_output_capacity"
    return Projection(
        request=request,
        compaction_reason=reason,
        working_context=latest_working_context(events),
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


def valid_compaction(summary: str, projection: Projection) -> bool:
    if not summary.strip():
        return False
    previous_size = sum(len(message.content) for message in projection.request.messages)
    return len(summary.strip()) < previous_size


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
