from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from nextloop import (
    EVENT_SCHEMA_VERSION,
    EventType,
    InMemoryEventStore,
    InvariantViolation,
    NewEvent,
    PayloadErasedError,
    SQLiteEventStore,
)
from nextloop.events.upcast import upcast_event

FIXTURE = Path(__file__).parent / "fixtures" / "events-v1.0.sqlite3"


def copied_fixture(tmp_path):
    path = tmp_path / "events-v1.0.sqlite3"
    shutil.copyfile(FIXTURE, path)
    return path


@pytest.mark.asyncio
async def test_real_1_0_fixture_upcasts_without_rewriting_store_bytes(tmp_path):
    path = copied_fixture(tmp_path)
    before = path.read_bytes()
    store = SQLiteEventStore(str(path))
    try:
        events = await store.load_recovery("complete")
        assert events
        assert EVENT_SCHEMA_VERSION == "1.1"
        assert all(event.schema_version == "1.1" for event in events)
        assert all(event.schema_version == "1.1" for event in await store.load_surface("complete"))
        assert all(upcast_event(event) == event for event in events)

        tool_calls = [event for event in events if event.event_type == EventType.TOOL_CALL]
        assert len(tool_calls) == 2
        assert len({event.step for event in tool_calls}) == 2
        assert len({event.payload["operation_id"] for event in tool_calls}) == 2
        assistant_operations = {
            (event.step, call["call_id"]): call["operation_id"]
            for event in events
            if event.event_type == EventType.ASSISTANT_MESSAGE
            for call in event.payload["message"].get("tool_calls", ())
        }
        for event in tool_calls:
            assert (
                event.payload["operation_id"]
                == assistant_operations[(event.step, event.payload["call_id"])]
            )

        compactions = [event for event in events if event.event_type == EventType.COMPACTION]
        assert len(compactions) == 1
        assert compactions[0].payload["source_seqs"] is None
    finally:
        store.close()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_idempotent_append_compares_raw_not_upcast_payload(tmp_path):
    store = SQLiteEventStore(str(copied_fixture(tmp_path)))
    try:
        events = await store.load_recovery("complete")
        tool = next(event for event in events if event.event_type == EventType.TOOL_CALL)
        changed_representation = NewEvent(
            event_type=tool.event_type,
            event_id=tool.event_id,
            turn=tool.turn,
            step=tool.step,
            attempt=tool.attempt,
            surface_op=tool.surface_op,
            payload=tool.payload,
        )
        with pytest.raises(InvariantViolation, match="different content"):
            await store.append("complete", tool.writer_epoch, tool.seq, [changed_representation])

        first = events[0]
        original = NewEvent(
            event_type=first.event_type,
            event_id=first.event_id,
            turn=first.turn,
            step=first.step,
            attempt=first.attempt,
            surface_op=first.surface_op,
            payload=first.payload,
        )
        repeated = await store.append("complete", first.writer_epoch, first.seq, [original])
        assert repeated[0].event_id == first.event_id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_erased_payload_identity_fails_closed_without_content_comparison():
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("erased")
    draft = NewEvent(
        EventType.TURN_START,
        turn=1,
        step=0,
        payload={"_nextloop_erased": True},
    )
    await store.append("erased", epoch, 1, [draft])

    with pytest.raises(PayloadErasedError):
        await store.append("erased", epoch, 1, [draft])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_idempotent_append_rejects_unknown_stored_schema(backend, tmp_path):
    store = (
        InMemoryEventStore()
        if backend == "memory"
        else SQLiteEventStore(str(tmp_path / "unknown.sqlite3"))
    )
    epoch, _, _ = await store.attach_writer("s")
    draft = NewEvent(EventType.TURN_START, turn=1, step=0)
    await store.append("s", epoch, 1, [draft])
    if backend == "memory":
        store._events["s"][0] = replace(store._events["s"][0], schema_version="9.9")
    else:
        store._connection.execute("UPDATE session_events SET schema_version = '9.9'")

    with pytest.raises(InvariantViolation, match="Unsupported Event schema"):
        await store.append("s", epoch, 1, [draft])

    if backend == "sqlite":
        store.close()


@pytest.mark.asyncio
async def test_real_1_1_fixture_preserves_operation_origin_and_lineage(tmp_path):
    path = tmp_path / "events-v1.1.sqlite3"
    shutil.copyfile(FIXTURE.with_name("events-v1.1.sqlite3"), path)
    before = path.read_bytes()
    store = SQLiteEventStore(str(path))
    try:
        approval = await store.load_recovery("approval")
        reconcile = await store.load_recovery("reconcile")
        assert all(event.schema_version == "1.1" for event in [*approval, *reconcile])
        assert all(upcast_event(event) == event for event in [*approval, *reconcile])
        dispatched = next(event for event in approval if event.event_type == EventType.TOOL_CALL)
        result = next(event for event in approval if event.event_type == EventType.TOOL_RESULT)
        assert result.payload["operation_id"] == dispatched.payload["operation_id"]
        assert (result.payload["origin_turn"], result.payload["origin_step"]) == (1, 1)
        compaction = next(event for event in approval if event.event_type == EventType.COMPACTION)
        assert compaction.payload["source_seqs"][0][0] == 1
        reconciliation = next(
            event for event in reconcile if event.event_type == EventType.RECONCILIATION_RESULT
        )
        assert reconciliation.payload["outcome"]["release_resources"] == ["dynamic"]
    finally:
        store.close()
    assert path.read_bytes() == before
