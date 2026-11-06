from __future__ import annotations

import pytest

from nextloop.adapters.sqlite import SQLiteEventStore
from nextloop.contracts import EventType, InvariantViolation, NewEvent, SurfaceOp


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_surface_and_writer_fence(tmp_path):
    path = str(tmp_path / "events.sqlite3")
    store = SQLiteEventStore(path)
    epoch, seq, turn = await store.attach_writer("s")
    assert (epoch, seq, turn) == (1, 0, 0)
    drafts = [
        NewEvent(EventType.TURN_START, turn=1, step=0),
        NewEvent(
            EventType.USER_MESSAGE,
            turn=1,
            step=1,
            surface_op=SurfaceOp.APPEND,
            payload={"message": {"role": "user", "content": "before"}},
        ),
        NewEvent(
            EventType.COMPACTION,
            turn=1,
            step=1,
            surface_op=SurfaceOp.EPOCH,
            payload={"summary": "short"},
        ),
        NewEvent(
            EventType.USER_MESSAGE,
            turn=1,
            step=2,
            surface_op=SurfaceOp.APPEND,
            payload={"message": {"role": "user", "content": "after"}},
        ),
    ]
    await store.append("s", epoch, 1, drafts)
    surface = await store.load_surface("s")
    assert [
        event.payload.get("summary", event.payload.get("message", {}).get("content"))
        for event in surface
    ] == [
        "short",
        "after",
    ]
    store.close()

    reopened = SQLiteEventStore(path)
    epoch2, seq2, turn2 = await reopened.attach_writer("s")
    assert (epoch2, seq2, turn2) == (2, 4, 1)
    with pytest.raises(InvariantViolation):
        await reopened.append("s", epoch, seq2 + 1, [NewEvent(EventType.STEP_END, turn=1, step=1)])
    assert len(await reopened.load_recovery("s")) == 4
    reopened.close()


@pytest.mark.asyncio
async def test_sqlite_idempotent_commit(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite3"))
    epoch, _, _ = await store.attach_writer("s")
    draft = NewEvent(EventType.TURN_START, turn=1, step=0)
    first = await store.append("s", epoch, 1, [draft])
    second = await store.append("s", epoch, 1, [draft])
    assert first[0].event_id == second[0].event_id
    assert len(await store.load_recovery("s")) == 1
    store.close()
