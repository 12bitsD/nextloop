from __future__ import annotations

from collections.abc import Awaitable

from .contracts import EventStore, EventType, InvariantViolation, NewEvent, SurfaceOp, new_id


async def check_event_store(store: EventStore) -> None:
    session_id = new_id("store_contract")
    epoch, seq, turn = await store.attach_writer(session_id)
    assert (epoch, seq, turn) == (1, 0, 0)
    drafts = [
        NewEvent(EventType.TURN_START, turn=1, step=0),
        NewEvent(
            EventType.USER_MESSAGE,
            turn=1,
            step=1,
            surface_op=SurfaceOp.APPEND,
            payload={"message": {"role": "user", "content": "hello"}},
        ),
    ]
    committed = await store.append(session_id, epoch, 1, drafts)
    assert [event.seq for event in committed] == [1, 2]
    repeated = await store.append(session_id, epoch, 1, drafts)
    assert [event.event_id for event in repeated] == [event.event_id for event in committed]
    assert len(await store.load_recovery(session_id)) == 2

    changed = NewEvent(
        EventType.USER_MESSAGE,
        event_id=drafts[1].event_id,
        turn=1,
        step=1,
        surface_op=SurfaceOp.APPEND,
        payload={"message": {"role": "user", "content": "changed"}},
    )
    await _expect_invariant(store.append(session_id, epoch, 1, [drafts[0], changed]))
    assert len(await store.load_recovery(session_id)) == 2

    next_epoch, _, _ = await store.attach_writer(session_id)
    assert next_epoch == 2
    await _expect_invariant(
        store.append(
            session_id,
            epoch,
            3,
            [NewEvent(EventType.STEP_END, turn=1, step=1)],
        )
    )
    assert len(await store.load_recovery(session_id)) == 2

    await store.append(
        session_id,
        next_epoch,
        3,
        [NewEvent(EventType.COMPACTION, turn=1, step=1, surface_op=SurfaceOp.EPOCH)],
    )
    surface = await store.load_surface(session_id)
    assert len(surface) == 1
    assert surface[0].event_type == EventType.COMPACTION


async def _expect_invariant(operation: Awaitable[object]) -> None:
    try:
        await operation
    except InvariantViolation:
        return
    raise AssertionError("Expected InvariantViolation")
