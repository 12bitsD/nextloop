from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from nextloop import EventType, NewEvent, SQLiteEventStore


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
    epoch, _, _ = await store.attach_writer("s")
    committed = await store.append("s", epoch, 1, [NewEvent(EventType.TURN_START, turn=1, step=0)])
    checked = threading.Event()
    resume_read = threading.Event()
    original_check = store._ensure_active_locked

    def pause_after_check(session_id):
        original_check(session_id)
        checked.set()
        assert resume_read.wait(timeout=2)

    store._ensure_active_locked = pause_after_check
    reading = asyncio.create_task(
        getattr(store, operation)(
            "s", *((committed[0].event_id,) if operation == "get_event" else ())
        )
    )
    await asyncio.to_thread(checked.wait)
    other = SQLiteEventStore(path)
    deleting = asyncio.create_task(other.delete_session("s"))
    resume_read.set()

    if operation == "get_event":
        event = await reading
        assert event is not None
        assert event.event_id == committed[0].event_id
    else:
        assert await reading == 1
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
