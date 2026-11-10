from __future__ import annotations

from collections.abc import Sequence

from ..contracts import EventType, InvariantViolation, Message, NewEvent, SurfaceOp
from .project import message_to_data


def user_input_events(
    inputs: Sequence[tuple[str, str]], *, turn: int, step: int
) -> tuple[NewEvent, ...]:
    unique: dict[str, tuple[str, str]] = {}
    for input_id, content in inputs:
        if input_id in unique and unique[input_id][1] != content:
            raise InvariantViolation("input_id reused with different content")
        unique.setdefault(input_id, (input_id, content))
    return tuple(
        NewEvent(
            EventType.USER_MESSAGE,
            turn=turn,
            step=step,
            surface_op=SurfaceOp.APPEND,
            payload={
                "input_id": input_id,
                "message": message_to_data(Message(role="user", content=content)),
            },
        )
        for input_id, content in unique.values()
    )
