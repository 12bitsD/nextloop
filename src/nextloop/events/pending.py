from __future__ import annotations

from collections.abc import Sequence

from ..contracts import Event, EventType, PendingInput


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
