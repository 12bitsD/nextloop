from __future__ import annotations

import pytest

from nextloop.adapters.memory import InMemoryEventStore
from nextloop.adapters.sqlite import SQLiteEventStore
from nextloop.contracts import EventType, InvariantViolation, NewEvent
from nextloop.testing import (
    DuplicateEventIdStore,
    UncheckedWriterStore,
    WrongSurfaceStore,
    check_event_store,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryEventStore()
        return
    value = SQLiteEventStore(str(tmp_path / "contract.sqlite3"))
    yield value
    value.close()


@pytest.mark.asyncio
async def test_public_store_contract(store):
    corrupt_schema = None
    if isinstance(store, SQLiteEventStore):
        session_ids = []

        def corrupt_schema(session_id):
            session_ids.append(session_id)
            store._connection.execute(
                "UPDATE session_events SET schema_version = 'future' WHERE session_id = ?",
                (session_id,),
            )

        await check_event_store(store, corrupt_schema=corrupt_schema)
        assert session_ids
    else:
        await check_event_store(store)


class Unserializable:
    def __deepcopy__(self, memo):
        raise ValueError("cannot copy")


@pytest.mark.asyncio
async def test_failed_batch_is_atomic(store):
    epoch, _, _ = await store.attach_writer("s")
    drafts = [
        NewEvent(EventType.TURN_START, turn=1, step=0),
        NewEvent(EventType.STEP_START, turn=1, step=1, payload={"bad": Unserializable()}),
    ]
    with pytest.raises((TypeError, ValueError)):
        await store.append("s", epoch, 1, drafts)
    assert await store.load_recovery("s") == ()


@pytest.mark.asyncio
async def test_sqlite_rejects_unknown_event_schema(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "schema.sqlite3"))
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, [NewEvent(EventType.TURN_START, turn=1, step=0)])
    store._connection.execute("UPDATE session_events SET schema_version = 'future'")

    with pytest.raises(InvariantViolation, match="future"):
        await store.load_recovery("s")
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken_store",
    [UncheckedWriterStore(), DuplicateEventIdStore(), WrongSurfaceStore()],
)
async def test_public_store_contract_rejects_known_bad_implementations(broken_store):
    with pytest.raises(AssertionError):
        await check_event_store(broken_store)
