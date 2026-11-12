from __future__ import annotations

from collections.abc import Sequence

from ..contracts import Event, EventType, InvariantViolation, PendingInput, TurnOutcome, TurnStatus


def pending_inputs(events: Sequence[Event]) -> tuple[PendingInput, ...]:
    pending: dict[str, PendingInput] = {}
    consumed: set[str] = set()
    for event in events:
        if event.event_type == EventType.INPUT_ACCEPTED:
            input_id = str(event.payload.get("input_id", ""))
            if input_id:
                pending[input_id] = PendingInput(
                    input_id=input_id,
                    turn=event.turn,
                    seq=event.seq,
                    content=str(event.payload.get("content", "")),
                )
        elif event.event_type == EventType.USER_MESSAGE:
            input_id = str(event.payload.get("input_id", ""))
            if input_id:
                consumed.add(input_id)
    return tuple(item for input_id, item in pending.items() if input_id not in consumed)


def completed_input_outcome(
    events: Sequence[Event], session_id: str, input_id: str, content: str
) -> TurnOutcome | None:
    accepted_turns = []
    for event in events:
        if (
            event.event_type != EventType.INPUT_ACCEPTED
            or event.payload.get("input_id") != input_id
        ):
            continue
        if event.payload.get("content", "") != content:
            raise InvariantViolation("input_id reused with different content")
        accepted_turns.append(event.turn)
    if pending_inputs(events):
        return None
    for accepted_turn in reversed(accepted_turns):
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
