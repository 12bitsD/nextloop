from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..contracts import (
    Event,
    EventType,
    InvariantViolation,
    Message,
    NewEvent,
    PayloadErasedError,
    PendingInput,
    SurfaceOp,
    ToolCall,
)
from .upcast import validate_event_schema

SURFACE_LOG_INCLUDED_TYPES = (EventType.TOOL_BATCH_REJECTED, EventType.INPUT_ACCEPTED)
SURFACE_LOG_INCLUDED_SQL = ", ".join(
    f"'{event_type.value}'" for event_type in SURFACE_LOG_INCLUDED_TYPES
)


def is_surface_event(event_type: EventType, surface_op: SurfaceOp) -> bool:
    return surface_op != SurfaceOp.NONE or event_type in SURFACE_LOG_INCLUDED_TYPES


def is_uncertain_result(event: Event) -> bool:
    outcome = event.payload.get("outcome", {})
    kind = outcome.get("kind")
    return kind in ("uncertain_hanging", "uncertain_settled") or (
        kind == "cancelled" and not outcome.get("settled", False)
    )


def with_origin(event: NewEvent, turn: int, step: int, kind: str | None = None) -> NewEvent:
    payload = {**event.payload, "origin_turn": turn, "origin_step": step}
    if kind is not None:
        payload["origin_kind"] = kind
    return replace(event, payload=payload)


def verify_idempotent_append(
    existing: Sequence[Event], drafts: Sequence[NewEvent]
) -> Sequence[Event]:
    by_id = {event.event_id: event for event in existing}
    committed = []
    for draft in drafts:
        event = by_id.get(draft.event_id)
        if event is None:
            raise InvariantViolation("Expected sequence does not match Session tail")
        validate_event_schema(event)
        if event.payload == {"_nextloop_erased": True}:
            raise PayloadErasedError(f"Event payload is erased: {event.event_id}")
        if (
            event.event_type != draft.event_type
            or event.turn != draft.turn
            or event.step != draft.step
            or event.attempt != draft.attempt
            or event.surface_op != draft.surface_op
            or event.payload != dict(draft.payload)
        ):
            raise InvariantViolation("Event ID reused with different content")
        committed.append(event)
    return tuple(committed)


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


@dataclass(frozen=True)
class ProjectionFacts:
    rejected_steps: frozenset[tuple[int, int]]
    pending: tuple[PendingInput, ...]
    approval_result_seq: int
    working_context: Message | None


def fold_projection_facts(events: Sequence[Event]) -> ProjectionFacts:
    rejected_steps: set[tuple[int, int]] = set()
    pending: dict[str, PendingInput] = {}
    approval_result_seq = 0
    working_context: Message | None = None
    for event in events:
        if event.event_type == EventType.INPUT_ACCEPTED:
            input_id = str(event.payload.get("input_id", ""))
            if input_id:
                pending.pop(input_id, None)
                pending[input_id] = PendingInput(
                    input_id=input_id,
                    turn=event.turn,
                    seq=event.seq,
                    content=str(event.payload.get("content", "")),
                )
        elif event.event_type == EventType.USER_MESSAGE:
            input_id = str(event.payload.get("input_id", ""))
            if input_id:
                pending.pop(input_id, None)
            marker = event.payload.get("working_context")
            if isinstance(marker, Mapping):
                action = marker.get("action")
                if action == "set":
                    working_context = message_from_data(event.payload["message"])
                elif action in ("clear", "expired"):
                    working_context = None
        elif event.event_type == EventType.TOOL_BATCH_REJECTED:
            rejected_steps.add((event.turn, event.step))
        elif (
            event.event_type == EventType.TOOL_RESULT
            and event.payload.get("origin_kind") == "approval"
        ):
            approval_result_seq = max(approval_result_seq, event.seq)
    return ProjectionFacts(
        rejected_steps=frozenset(rejected_steps),
        pending=tuple(pending.values()),
        approval_result_seq=approval_result_seq,
        working_context=working_context,
    )
