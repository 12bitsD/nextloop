from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from helpers import FakeDelivery, FakeProvider
from test_approval import approval_tools
from test_loop import call, context
from test_outbox import closed_turn

from nextloop import (
    AgentLoop,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    SQLiteOutbox,
    ToolCallTrace,
    TurnTrace,
    project_traces,
)
from nextloop.testing import InjectedCrash


@pytest.mark.asyncio
async def test_exports_are_serialized_and_cursor_never_regresses(tmp_path):
    for same_instance in (True, False):
        store = InMemoryEventStore()
        epoch, _, _ = await store.attach_writer("s")
        await store.append("s", epoch, 1, [*closed_turn(1), *closed_turn(2)])
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(record, same_instance=same_instance, started=started, release=release):
            if same_instance or record.turn == 2:
                started.set()
                await release.wait()

        path = str(tmp_path / f"outbox-{same_instance}.sqlite3")
        first = SQLiteOutbox(store, path, [slow])
        second = first if same_instance else SQLiteOutbox(store, path, [])
        first_task = asyncio.create_task(first.export_pending())
        await asyncio.sleep(0)
        if first_task.done():
            await first_task
        await asyncio.wait_for(started.wait(), timeout=1)
        second_task = asyncio.create_task(second.export_pending())
        await asyncio.sleep(0.02)
        assert not second_task.done()

        release.set()
        first_report, second_report = await asyncio.gather(first_task, second_task)

        assert first_report.exported == 2
        assert second_report.exported == 0
        assert second.watermark("s") == 6
        assert (await second.export_pending()).exported == 0
        first.close()
        if not same_instance:
            second.close()


@pytest.mark.asyncio
async def test_exporter_timeouts_are_bounded_and_recorded(tmp_path):
    store = InMemoryEventStore()
    for session in ("slow", "fast"):
        epoch, _, _ = await store.attach_writer(session)
        await store.append(session, epoch, 1, closed_turn())

    async def blocked_async(record):
        if record.session_id == "slow":
            await asyncio.Event().wait()

    outbox = SQLiteOutbox(
        store,
        str(tmp_path / "async.sqlite3"),
        [blocked_async],
        max_attempts=1,
        export_timeout_seconds=0.01,
    )
    report = await outbox.export_pending()

    assert report.exported == 1
    assert "fast" in report.session_ids
    assert [(gap.session_id, gap.from_seq, gap.to_seq) for gap in report.gaps] == [("slow", 0, 3)]
    outbox.close()

    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())

    def blocked_sync(record):
        time.sleep(10)

    outbox = SQLiteOutbox(
        store,
        str(tmp_path / "sync.sqlite3"),
        [blocked_sync],
        max_attempts=1,
        export_timeout_seconds=0.01,
    )
    started = time.monotonic()
    report = await outbox.export_pending()

    assert time.monotonic() - started < 1
    assert len(report.gaps) == 1
    assert report.exported == 0
    outbox.close()


@pytest.mark.asyncio
async def test_replay_after_restart_preserves_record_identity_and_content(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())
    first = []

    class BeforeCursor(SQLiteOutbox):
        def _advance(self, session_id, seq):
            raise InjectedCrash("before cursor")

    crashing = BeforeCursor(store, str(tmp_path / "outbox.sqlite3"), [first.append])
    with pytest.raises(InjectedCrash):
        await crashing.export_pending()
    crashing.close()
    second = []
    outbox = SQLiteOutbox(store, str(tmp_path / "outbox.sqlite3"), [second.append])

    assert (await outbox.export_pending()).exported == 1
    assert first == second
    outbox.close()


@pytest.mark.asyncio
async def test_delete_session_clears_cursor_attempts_and_gaps(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())

    def fail(record):
        raise RuntimeError("unavailable")

    outbox = SQLiteOutbox(store, str(tmp_path / "outbox.sqlite3"), [fail], max_attempts=1)
    await outbox.export_pending()
    assert outbox.gaps("s")

    outbox.delete_session("s")

    assert outbox.watermark("s") == 0
    assert outbox.gaps("s") == ()
    outbox.close()


@pytest.mark.asyncio
async def test_approval_wait_is_not_in_tool_duration():
    invoked = []
    tools = approval_tools(invoked)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))
    operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    await asyncio.sleep(0.05)

    await loop.resume(
        context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
    )

    traces = project_traces(await store.load_recovery("s"))
    tool = next(record for record in traces if isinstance(record, ToolCallTrace))
    turn = next(record for record in traces if isinstance(record, TurnTrace))
    assert tool.duration_ms < 50
    assert turn.duration_ms is not None


@pytest.mark.flaky(reason="Depends on wall-clock heartbeat scheduling; excluded from CI")
@pytest.mark.asyncio
async def test_live_export_heartbeat_prevents_duplicate_export(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(record):
        started.set()
        await release.wait()

    exported = []
    first = SQLiteOutbox(
        store,
        str(tmp_path / "outbox.sqlite3"),
        [slow],
        export_timeout_seconds=1,
        lock_timeout_seconds=0.03,
    )
    second = SQLiteOutbox(
        store,
        str(tmp_path / "outbox.sqlite3"),
        [exported.append],
        export_timeout_seconds=1,
        lock_timeout_seconds=0.03,
    )
    first_task = asyncio.create_task(first.export_pending())
    await asyncio.wait_for(started.wait(), timeout=1)
    second_task = asyncio.create_task(second.export_pending())
    await asyncio.sleep(0.08)
    assert not second_task.done()

    release.set()
    first_report, second_report = await asyncio.gather(first_task, second_task)

    assert first_report.exported == 1
    assert second_report.exported == 0
    assert exported == []
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_previous_owner_cannot_release_new_owner_lock(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())
    path = str(tmp_path / "outbox.sqlite3")
    first = SQLiteOutbox(store, path, [], lock_timeout_seconds=0.01)
    second = SQLiteOutbox(store, path, [])
    third = SQLiteOutbox(store, path, [])
    await first._acquire("s")
    with first._lock, first._connection:
        first._connection.execute("UPDATE outbox_locks SET locked_at = 0 WHERE session_id = 's'")
    await second._acquire("s")

    await first._release("s")
    third_task = asyncio.create_task(third._acquire("s"))
    await asyncio.sleep(0.03)

    assert not third_task.done()
    await second._release("s")
    await asyncio.wait_for(third_task, timeout=1)
    await third._release("s")
    first.close()
    second.close()
    third.close()


@pytest.mark.asyncio
async def test_lock_timeout_preserves_integrity_error(tmp_path):
    store = InMemoryEventStore()
    path = str(tmp_path / "outbox.sqlite3")
    first = SQLiteOutbox(store, path, [])
    second = SQLiteOutbox(store, path, [])
    await first._acquire("s")

    with pytest.raises(TimeoutError) as raised:
        await second._acquire("s")

    assert isinstance(raised.value.__cause__, sqlite3.IntegrityError)
    await first._release("s")
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_heartbeat_failure_still_releases_lock_without_advancing_cursor(tmp_path):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())

    class FailedHeartbeat(SQLiteOutbox):
        def _touch(self, session_id):
            raise RuntimeError("renewal failed")

    async def slow(record):
        await asyncio.sleep(0.03)

    outbox = FailedHeartbeat(
        store, str(tmp_path / "outbox.sqlite3"), [slow], lock_timeout_seconds=0.01
    )

    with pytest.raises(RuntimeError, match="renewal failed"):
        await outbox.export_pending()

    assert outbox.watermark("s") == 0
    assert outbox._connection.execute("SELECT * FROM outbox_locks").fetchone() is None
    outbox.close()


@pytest.mark.asyncio
async def test_concurrent_old_lock_table_migration_adds_owner_once(tmp_path):
    store = InMemoryEventStore()
    path = str(tmp_path / "outbox.sqlite3")
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE outbox_locks (session_id TEXT PRIMARY KEY, locked_at REAL NOT NULL)"
    )
    connection.close()

    barrier = threading.Barrier(2)

    async def build():
        await asyncio.to_thread(barrier.wait)
        return await asyncio.to_thread(SQLiteOutbox, store, path, [])

    first, second = await asyncio.gather(build(), build())

    columns = first._connection.execute("PRAGMA table_info(outbox_locks)").fetchall()
    assert [row["name"] for row in columns].count("owner_token") == 1
    first.close()
    second.close()
