from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import httpx
import pytest
from helpers import CountingEventStore as CountingStore
from helpers import MinimalEventStore as MinimalStore
from test_outbox import closed_turn
from test_providers import request

from nextloop import (
    EventType,
    InMemoryEventStore,
    InvariantViolation,
    NewEvent,
    OpenAIProvider,
    SQLiteEventStore,
    SQLiteOutbox,
    SurfaceOp,
    ToolCall,
    ToolRegistry,
    check_event_store,
    with_result_reads,
)
from nextloop.adapters._sqlite import write_transaction
from nextloop.adapters.observing import ObservingEventStore
from nextloop.testing import FaultInjectingEventStore


async def committed_result(store, content="hello"):
    epoch, _, _ = await store.attach_writer("s")
    event = NewEvent(
        EventType.TOOL_RESULT,
        turn=1,
        step=1,
        surface_op=SurfaceOp.APPEND,
        payload={"outcome": {"kind": "completed", "content": content}},
    )
    committed = await store.append("s", epoch, 1, [event])
    return committed[0]


async def read_page(store, event):
    runtime = with_result_reads(ToolRegistry(), store)
    arguments = {"event_id": event.event_id, "offset": 0, "limit": 256}
    return await runtime.execute(
        ToolCall("read", "read_tool_result", json.dumps(arguments)),
        arguments,
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_result_read_uses_point_query_without_recovery_scan():
    store = CountingStore()
    event = await committed_result(store)
    reads = store.recovery_reads

    response = await read_page(store, event)

    assert "hello" in response.content
    assert store.recovery_reads == reads


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["observing", "fault", "none"])
async def test_missing_capabilities_fall_back_through_wrappers(wrapper):
    base = MinimalStore()
    if wrapper == "observing":
        store = ObservingEventStore(base, [])
    elif wrapper == "fault":
        store = FaultInjectingEventStore(base)
    else:
        store = base
    event = await committed_result(store)
    reads = base.recovery_reads

    response = await read_page(store, event)

    assert "hello" in response.content
    assert base.recovery_reads == reads + 1
    if wrapper == "observing":
        await store.close()


@pytest.mark.asyncio
async def test_idle_outbox_skips_recovery_read():
    store = CountingStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, closed_turn())
    outbox = SQLiteOutbox(store, ":memory:", [])
    await outbox.export_pending()
    reads = store.recovery_reads

    report = await outbox.export_pending()

    assert report.exported == 0
    assert store.recovery_reads == reads
    outbox.close()


@pytest.mark.asyncio
async def test_delete_contention_does_not_block_event_loop(tmp_path, monkeypatch):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite3"), owner_id="owner")
    epoch, _, _ = await store.attach_writer("s")
    started = threading.Event()
    release = threading.Event()

    def blocked_purge(connection):
        started.set()
        release.wait(2)

    monkeypatch.setattr("nextloop.adapters.sqlite.purge_deleted_bytes", blocked_purge)
    deleting = asyncio.create_task(store.delete_session("s"))
    await asyncio.to_thread(started.wait)
    renewing = asyncio.create_task(store.renew_writer("s", epoch))

    await asyncio.sleep(0.05)
    assert not deleting.done()
    release.set()
    await asyncio.gather(deleting, renewing, return_exceptions=True)
    store.close()


def test_begin_failure_is_not_replaced_by_rollback():
    class Connection:
        in_transaction = False

        def __init__(self):
            self.calls = []

        def execute(self, statement):
            self.calls.append(statement)
            if statement == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("begin failed")

    connection = Connection()
    with (
        pytest.raises(sqlite3.OperationalError, match="begin failed"),
        write_transaction(connection),
    ):
        pass
    assert "ROLLBACK" not in connection.calls


def test_rollback_failure_preserves_commit_error():
    class Connection:
        in_transaction = True

        def execute(self, statement):
            if statement == "COMMIT":
                self.in_transaction = True
                raise sqlite3.OperationalError("commit failed")
            if statement == "ROLLBACK":
                raise sqlite3.OperationalError("rollback failed")

    with (
        pytest.raises(sqlite3.OperationalError, match="commit failed") as caught,
        write_transaction(Connection()),
    ):
        pass
    assert any("rollback failed" in note for note in caught.value.__notes__)


@pytest.mark.asyncio
async def test_owned_openai_client_is_shared_and_closed_once(monkeypatch):
    chunk = {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
    body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    created = []
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        client = real_client(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr("nextloop.adapters.openai.httpx.AsyncClient", factory)
    provider = OpenAIProvider(
        model_id="test-model", base_url="https://provider.test/v1", api_key="secret"
    )

    await asyncio.gather(
        provider.complete(request(), attempt=1, cancel=asyncio.Event()),
        provider.complete(request(), attempt=1, cancel=asyncio.Event()),
    )
    assert len(created) == 1
    assert not created[0].is_closed

    await provider.close()
    await provider.close()

    assert created[0].is_closed


@pytest.mark.asyncio
async def test_external_client_remains_caller_owned():
    client = httpx.AsyncClient()
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        http_client=client,
    )

    await provider.close()

    assert not client.is_closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["observing", "fault"])
async def test_leased_wrappers_pass_store_conformance(wrapper):
    base = InMemoryEventStore(owner_id="owner", lease_seconds=10)
    store = (
        ObservingEventStore(base, []) if wrapper == "observing" else FaultInjectingEventStore(base)
    )

    await check_event_store(store)

    assert store.lease_seconds == base.lease_seconds
    if wrapper == "observing":
        await store.close()


@pytest.mark.asyncio
async def test_external_sqlite_write_lock_does_not_block_event_loop(tmp_path):
    path = str(tmp_path / "contended.sqlite3")
    holder = sqlite3.connect(path)
    contender = SQLiteEventStore(path)
    holder.execute("BEGIN IMMEDIATE")
    started = asyncio.get_running_loop().time()
    attaching = asyncio.create_task(contender.attach_writer("s"))

    await asyncio.sleep(0.05)
    elapsed = asyncio.get_running_loop().time() - started
    holder.execute("ROLLBACK")
    await attaching

    assert elapsed < 0.2
    holder.close()
    contender.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["observing", "fault"])
async def test_checked_attach_wrapper_rejection_does_not_advance_epoch(wrapper):
    base = InMemoryEventStore()
    store = (
        ObservingEventStore(base, []) if wrapper == "observing" else FaultInjectingEventStore(base)
    )

    def reject(history):
        raise InvariantViolation("not allowed")

    with pytest.raises(InvariantViolation, match="not allowed"):
        await store.attach_writer_checked("s", reject)
    epoch, last_seq, last_turn = await store.attach_writer("s")

    assert (epoch, last_seq, last_turn) == (1, 0, 0)
    if wrapper == "observing":
        await store.close()
