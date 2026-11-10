from __future__ import annotations

import pytest

from nextloop import Event, EventType, InvariantViolation, SurfaceOp
from nextloop.events.project import compaction_events

TIMESTAMP = "2026-01-01T00:00:00+00:00"


def event(event_type, seq, turn, step, payload):
    return Event(
        session_id="s",
        seq=seq,
        turn=turn,
        step=step,
        attempt=0,
        writer_epoch=1,
        event_id=f"evt-{seq}",
        event_type=event_type,
        surface_op=SurfaceOp.APPEND,
        payload=payload,
        occurred_at=TIMESTAMP,
    )


def assistant(operation_id="op-1"):
    return event(
        EventType.ASSISTANT_MESSAGE,
        1,
        1,
        1,
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "call_id": "call-1",
                        "name": "write",
                        "arguments": "{}",
                        "operation_id": operation_id,
                    }
                ],
            }
        },
    )


def result(operation_id="op-1"):
    return event(
        EventType.TOOL_RESULT,
        2,
        2,
        1,
        {
            "call_id": "call-1",
            "operation_id": operation_id,
            "origin_turn": 1,
            "origin_step": 1,
            "message": {"role": "tool", "content": "done", "tool_call_id": "call-1"},
            "outcome": {"kind": "completed", "content": "done"},
        },
    )


def test_compaction_records_real_source_range_and_pairs_across_turns():
    events = compaction_events("short", [assistant(), result()], turn=3, step=1)

    assert events[0].payload["source_seqs"] == [[1, 2]]


def test_compaction_rejects_unpaired_tool_intent():
    with pytest.raises(InvariantViolation, match="unresolved Tool Intent"):
        compaction_events("short", [assistant()], turn=2, step=1)
