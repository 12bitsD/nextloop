from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest
from test_outbox import closed_turn

from nextloop import (
    EventType,
    InMemoryEventStore,
    NewEvent,
    ObservingEventStore,
    SessionDeletedError,
    SQLiteEventStore,
    SQLiteOutbox,
    SurfaceOp,
    WriterLeaseError,
)
from nextloop.testing import InjectedCrash


def content_event(content: str, *, seq: int = 1) -> NewEvent:
    return NewEvent(
        EventType.USER_MESSAGE,
        turn=1,
        step=seq,
        surface_op=SurfaceOp.APPEND,
        payload={"message": {"role": "user", "content": content}},
    )


async def seed(store, session_id: str, content: str = "content") -> int:
    epoch, _, _ = await store.attach_writer(session_id)
    await store.append(session_id, epoch, 1, [content_event(content)])
    return epoch


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryEventStore(owner_id="owner")
        return
    value = SQLiteEventStore(str(tmp_path / "events.sqlite3"), owner_id="owner")
    yield value
    value.close()


@pytest.mark.asyncio
async def test_delete_is_reentrant_and_fences_reads_and_writers(store):
    epoch = await seed(store, "deleted")
    await seed(store, "keep", "unrelated")

    await store.delete_session("deleted")
    await store.delete_session("deleted")

    rejected = (
        store.attach_writer("deleted"),
        store.check_writer("deleted", epoch),
        store.renew_writer("deleted", epoch),
        store.has_writer("deleted"),
        store.load_surface("deleted"),
        store.load_recovery("deleted"),
        store.append("deleted", epoch, 2, []),
        store.append("deleted", epoch, 2, [content_event("recreated")]),
    )
    for operation in rejected:
        with pytest.raises(SessionDeletedError):
            await operation
    assert await store.session_ids() == ("keep",)
    assert len(await store.load_recovery("keep")) == 1
    if isinstance(store, SQLiteEventStore):
        assert (
            store._connection.execute(
                "SELECT state FROM session_tombstones WHERE session_id = 'deleted'"
            ).fetchone()["state"]
            == "deleted"
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM session_events WHERE session_id = 'deleted'"
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM session_writers WHERE session_id = 'deleted'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_deleting_tombstone_fails_closed_and_resumes_after_reopen(tmp_path):
    path = str(tmp_path / "events.sqlite3")
    store = SQLiteEventStore(path)
    await seed(store, "s", "sensitive")
    store._connection.execute(
        "CREATE TABLE session_tombstones (session_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
    )
    store._connection.execute(
        "INSERT INTO session_tombstones(session_id, state) VALUES ('s', 'deleting')"
    )
    store.close()

    reopened = SQLiteEventStore(path)
    with pytest.raises(SessionDeletedError, match="deleting"):
        await reopened.load_recovery("s")
    with pytest.raises(SessionDeletedError):
        await reopened.attach_writer("s")

    await reopened.delete_session("s")

    assert await reopened.session_ids() == ()
    with pytest.raises(SessionDeletedError, match="deleted"):
        await reopened.load_recovery("s")
    reopened.close()


def assert_canary_absent(path: str, canary: str) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(path + suffix)
        if candidate.exists():
            assert canary.encode() not in candidate.read_bytes(), candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
async def test_sqlite_and_outbox_purge_content_canaries(tmp_path, journal_mode):
    path = str(tmp_path / "events.sqlite3")
    outbox_path = str(tmp_path / "outbox.sqlite3")
    canary = "nextloop-delete-canary-7f3b"
    store = SQLiteEventStore(path)
    if journal_mode == "DELETE":
        store._connection.execute("PRAGMA journal_mode=DELETE")
    epoch, _, _ = await store.attach_writer("s")
    await store.append(
        "s",
        epoch,
        1,
        [
            content_event(canary),
            NewEvent(
                EventType.COMPACTION,
                turn=1,
                step=1,
                surface_op=SurfaceOp.EPOCH,
                payload={"summary": canary},
            ),
        ],
    )
    outbox = SQLiteOutbox(store, outbox_path, [])
    await outbox.export_pending()
    outbox._connection.execute(
        "INSERT INTO outbox_gaps(session_id, from_seq, to_seq, reason, created_at) "
        "VALUES ('s', 1, 2, ?, 'now')",
        (canary,),
    )

    await store.delete_session("s")
    outbox.delete_session("s")
    store.close()
    outbox.close()

    assert_canary_absent(path, canary)
    assert_canary_absent(outbox_path, canary)


@pytest.mark.asyncio
async def test_cleanup_crash_after_logical_delete_is_reentrant(tmp_path, monkeypatch):
    path = str(tmp_path / "events.sqlite3")
    canary = "nextloop-crash-canary-91ac"
    store = SQLiteEventStore(path)
    await seed(store, "s", canary)

    def crash(connection):
        raise InjectedCrash("after logical deletion")

    monkeypatch.setattr("nextloop.adapters.sqlite.purge_deleted_bytes", crash)
    with pytest.raises(InjectedCrash):
        await store.delete_session("s")
    assert (
        store._connection.execute(
            "SELECT state FROM session_tombstones WHERE session_id = 's'"
        ).fetchone()["state"]
        == "deleted"
    )
    store.close()

    monkeypatch.undo()
    reopened = SQLiteEventStore(path)
    with pytest.raises(SessionDeletedError):
        await reopened.load_recovery("s")
    await reopened.delete_session("s")
    reopened.close()

    assert_canary_absent(path, canary)


@pytest.mark.asyncio
async def test_observing_delete_purges_only_deleted_session_after_failed_cleanup():
    class FailedCleanupStore(InMemoryEventStore):
        async def delete_session(self, session_id: str) -> None:
            await super().delete_session(session_id)
            raise RuntimeError("cleanup failed")

    started = asyncio.Event()
    release = asyncio.Event()
    exported = []

    async def slow_export(record):
        started.set()
        await release.wait()
        exported.append((record.session_id, record.turn))

    store = ObservingEventStore(FailedCleanupStore(), [slow_export])
    for session, turn in (("deleted", 1), ("deleted", 2), ("keep", 1)):
        epoch, last_seq, _ = await store.attach_writer(session)
        await store.append(session, epoch, last_seq + 1, closed_turn(turn))
    await asyncio.wait_for(started.wait(), timeout=1)

    deletion = asyncio.create_task(store.delete_session("deleted"))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await deletion
    await store.flush(timeout_seconds=1)

    assert exported == [("deleted", 1), ("keep", 1)]
    assert store.dropped_count == 1
    with pytest.raises(SessionDeletedError):
        await store.load_recovery("deleted")
    assert len(await store.load_recovery("keep")) == 3
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "retry", "gap"])
async def test_outbox_delete_cannot_be_resurrected_by_inflight_export(tmp_path, outcome):
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())
    started = asyncio.Event()
    release = asyncio.Event()

    async def exporter(record):
        started.set()
        await release.wait()
        if outcome != "success":
            raise RuntimeError("export failed")

    outbox = SQLiteOutbox(
        store,
        str(tmp_path / "outbox.sqlite3"),
        [exporter],
        max_attempts=1 if outcome == "gap" else 2,
    )
    exporting = asyncio.create_task(outbox.export_pending())
    await asyncio.wait_for(started.wait(), timeout=1)

    outbox.delete_session("s")
    assert outbox.watermark("s") == 0
    release.set()
    with pytest.raises(WriterLeaseError):
        await exporting

    for table in ("outbox_cursor", "outbox_attempts", "outbox_gaps", "outbox_locks"):
        assert (
            outbox._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = 's'"
            ).fetchone()[0]
            == 0
        )
    outbox.close()


@pytest.mark.asyncio
async def test_concurrent_delete_cannot_turn_snapshot_read_into_empty_history(tmp_path):
    path = str(tmp_path / "events.sqlite3")
    store = SQLiteEventStore(path)
    await seed(store, "s", "snapshot-content")
    checked = threading.Event()
    resume_read = threading.Event()
    original_check = store._ensure_active_locked

    def pause_after_check(session_id):
        original_check(session_id)
        checked.set()
        assert resume_read.wait(timeout=2)

    store._ensure_active_locked = pause_after_check
    reading = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(store.load_recovery("s"))))
    assert await asyncio.to_thread(checked.wait)
    other = SQLiteEventStore(path)
    deletion = asyncio.create_task(
        asyncio.to_thread(lambda: asyncio.run(other.delete_session("s")))
    )
    deleted = False
    for _ in range(200):
        probe = sqlite3.connect(path)
        table = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_tombstones'"
        ).fetchone()
        row = (
            probe.execute("SELECT state FROM session_tombstones WHERE session_id = 's'").fetchone()
            if table is not None
            else None
        )
        probe.close()
        if row is not None and row[0] == "deleted":
            deleted = True
            break
        await asyncio.sleep(0.01)
    resume_read.set()
    history = await reading
    await deletion

    assert deleted
    assert len(history) == 1
    await other.delete_session("s")
    with pytest.raises(SessionDeletedError):
        await other.load_recovery("s")
    other.close()
    store.close()
