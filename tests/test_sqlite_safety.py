from __future__ import annotations

import asyncio
import sqlite3

import pytest

from nextloop import EventType, NewEvent, SessionDeletedError, SQLiteEventStore


@pytest.mark.asyncio
async def test_delete_waits_for_short_external_write_lock(tmp_path):
    path = str(tmp_path / "delete-lock.sqlite3")
    store = SQLiteEventStore(path)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    deleting = asyncio.create_task(store.delete_session("s"))

    await asyncio.sleep(0.05)
    assert not deleting.done()
    holder.execute("ROLLBACK")
    await deleting

    holder.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_event", "last_seq"])
async def test_point_reads_keep_snapshot_during_delete(tmp_path, operation):
    path = str(tmp_path / f"{operation}-snapshot.sqlite3")
    store = SQLiteEventStore(path)
    other = SQLiteEventStore(path)
    for index in range(200):
        session_id = f"s{index}"
        epoch, _, _ = await store.attach_writer(session_id)
        committed = await store.append(
            session_id, epoch, 1, [NewEvent(EventType.TURN_START, turn=1, step=0)]
        )
        arguments = (committed[0].event_id,) if operation == "get_event" else ()
        reading = asyncio.create_task(getattr(store, operation)(session_id, *arguments))
        deleting = asyncio.create_task(other.delete_session(session_id))
        try:
            result = await reading
        except SessionDeletedError:
            pass
        else:
            if operation == "get_event":
                assert result is not None
                assert result.event_id == committed[0].event_id
            else:
                assert result == 1
        await deleting
    store.close()
    other.close()


@pytest.mark.asyncio
async def test_large_append_does_not_delay_independent_heartbeat(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "large-append.sqlite3"))
    epoch, _, _ = await store.attach_writer("s")
    events = [
        NewEvent(
            EventType.USER_MESSAGE,
            turn=1,
            step=1,
            payload={"message": {"role": "user", "content": "x" * 4096}},
        )
        for _ in range(5000)
    ]
    appending = asyncio.create_task(store.append("s", epoch, 1, events))

    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
    await appending

    store.close()
