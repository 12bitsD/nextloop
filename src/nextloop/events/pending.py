from __future__ import annotations

from collections.abc import Sequence

from ..contracts import Event, EventType, InvariantViolation, PendingInput, TurnOutcome, TurnStatus
from .codec import fold_projection_facts


def pending_inputs(events: Sequence[Event]) -> tuple[PendingInput, ...]:
    return fold_projection_facts(events).pending


def completed_input_outcome(
    events: Sequence[Event], session_id: str, input_id: str, content: str
) -> TurnOutcome | None:
    consumed_turns = []
    for event in events:
        if (
            event.event_type == EventType.INPUT_ACCEPTED
            and event.payload.get("input_id") == input_id
            and event.payload.get("content", "") != content
        ):
            raise InvariantViolation("input_id reused with different content")
        if event.event_type == EventType.USER_MESSAGE and event.payload.get("input_id") == input_id:
            consumed_turns.append(event.turn)
    for accepted_turn in reversed(consumed_turns):
        end = next(
            (
                event
                for event in events
                if event.turn == accepted_turn and event.event_type == EventType.TURN_END
            ),
            None,
        )
        if end is None:
            continue
        try:
            status = TurnStatus(end.payload["status"])
        except (KeyError, ValueError):
            continue
        if status not in (TurnStatus.SUCCEEDED, TurnStatus.DELIVERY_UNKNOWN):
            continue
        final_message_id = end.payload.get("final_message_id")
        error = end.payload.get("error")
        if final_message_id is not None:
            deliveries = [
                event
                for event in events
                if event.turn == accepted_turn
                and event.event_type in (EventType.DELIVERY_COMMITTED, EventType.DELIVERY_UNKNOWN)
                and event.payload.get("final_message_id") == final_message_id
            ]
            if deliveries and deliveries[-1].event_type == EventType.DELIVERY_UNKNOWN:
                status = TurnStatus.DELIVERY_UNKNOWN
                error = deliveries[-1].payload.get("error")
        return TurnOutcome(
            status=status,
            session_id=session_id,
            turn=accepted_turn,
            step=end.step,
            final_message_id=final_message_id,
            error=error,
        )
    return None
