from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ..contracts import (
    Event,
    EventType,
    InvariantViolation,
    NewEvent,
    PayloadErasedError,
    SurfaceOp,
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
