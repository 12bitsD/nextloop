from __future__ import annotations

import io
import json

import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import context, registry

from nextloop import (
    AgentLoop,
    EventType,
    InMemoryEventStore,
    ModelResponse,
    NewEvent,
    SQLiteOutbox,
    StdoutExporter,
)
from nextloop.testing import InjectedCrash


def closed_turn(turn=1):
    return [
        NewEvent(
            EventType.INPUT_ACCEPTED,
            turn=turn,
            step=0,
            payload={"input_id": f"in-{turn}", "content": "start"},
        ),
        NewEvent(EventType.TURN_START, turn=turn, step=0),
        NewEvent(
            EventType.TURN_END,
            turn=turn,
            step=1,
            payload={"status": "interrupted", "recovered": True},
        ),
    ]


@pytest.mark.asyncio
async def test_outbox_exports_closed_turns_once_with_otel_ids(tmp_path):
    store = InMemoryEventStore()
    await AgentLoop().run(
        context(store, FakeProvider([ModelResponse(content="done")]), registry(), FakeDelivery())
    )
    stream = io.StringIO()
    outbox = SQLiteOutbox(store, str(tmp_path / "outbox.sqlite3"), [StdoutExporter(stream)])

    report = await outbox.export_pending()

    assert report.exported > 0
    assert (await outbox.export_pending()).exported == 0
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert payloads
    assert len({payload["trace_id"] for payload in payloads}) == 1
    assert len({payload["span_id"] for payload in payloads}) == len(payloads)
    assert all(len(payload["trace_id"]) == 32 for payload in payloads)
    assert all(len(payload["span_id"]) == 16 for payload in payloads)
    outbox.close()


@pytest.mark.asyncio
async def test_active_turn_is_not_exported_and_closed_turn_needs_no_recover(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn()[:2])
    outbox = SQLiteOutbox(store, str(tmp_path / "outbox.sqlite3"), [])

    assert (await outbox.export_pending()).exported == 0

    epoch, last_seq, _ = await store.attach_writer("s")
    await store.append("s", epoch, last_seq + 1, closed_turn()[2:])
    report = await outbox.export_pending()

    assert report.exported == 1
    assert report.session_ids == ("s",)
    assert outbox.watermark("s") == 3
    outbox.close()


@pytest.mark.asyncio
async def test_crash_after_export_ack_before_cursor_replays_but_after_cursor_does_not(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())

    class BeforeCursor(SQLiteOutbox):
        def _advance(self, session_id, seq):
            raise InjectedCrash("before cursor")

    class AfterCursor(SQLiteOutbox):
        def _advance(self, session_id, seq):
            super()._advance(session_id, seq)
            raise InjectedCrash("after cursor")

    before = BeforeCursor(store, str(tmp_path / "before.sqlite3"), [lambda record: None])
    with pytest.raises(InjectedCrash):
        await before.export_pending()
    before.close()
    assert SQLiteOutbox(store, str(tmp_path / "before.sqlite3"), []).watermark("s") == 0
    replay = SQLiteOutbox(store, str(tmp_path / "before.sqlite3"), [])
    assert (await replay.export_pending()).exported == 1
    replay.close()

    after = AfterCursor(store, str(tmp_path / "after.sqlite3"), [lambda record: None])
    with pytest.raises(InjectedCrash):
        await after.export_pending()
    after.close()
    assert SQLiteOutbox(store, str(tmp_path / "after.sqlite3"), []).watermark("s") == 3
    deduplicated = SQLiteOutbox(store, str(tmp_path / "after.sqlite3"), [])
    assert (await deduplicated.export_pending()).exported == 0
    deduplicated.close()


@pytest.mark.asyncio
async def test_persistent_failures_become_auditable_gaps_and_advance(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, [*closed_turn(1), *closed_turn(2)])

    def fail(record):
        raise RuntimeError("export unavailable")

    outbox = SQLiteOutbox(store, str(tmp_path / "outbox.sqlite3"), [fail], max_attempts=1)

    first = await outbox.export_pending()
    second = await outbox.export_pending()
    third = await outbox.export_pending()

    assert first.exported == second.exported == third.exported == 0
    assert len(first.gaps) == 2
    assert len(second.gaps) == 0
    assert len(third.gaps) == 0
    assert len(outbox.gaps("s")) == 2
    assert outbox.watermark("s") == 6
    assert (await outbox.export_pending()).exported == 0
    outbox.close()
